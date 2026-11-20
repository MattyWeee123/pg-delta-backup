# pg-delta-backup

A CMU student project with Google's AlloyDB team: a tool that brings an old copy of a PostgreSQL database up to date by sending only the parts that changed, instead of copying everything again the way `pg_basebackup` does.

**The engine on `main` (November 20, 2026)** is the first phase's, restored from tag `v1-research-final`. On November 13 `main` restarted from a naive version so the team could rebuild each improvement step by step; a teammate rebuilt the pipelined exchange (PRs #43 and #44). A same-session race on two cloud VMs then measured the first phase's engine ahead on every arm (below), so it is back on `main`, with live progress added for the viewer. Everything from the first phase (591 commits, 93 experiments, the research) is on branch [`archive/v1-research`](https://github.com/MattyWeee123/pg-delta-backup/tree/archive/v1-research); [docs/PAST-RESULTS.md](docs/PAST-RESULTS.md) summarizes it.

## Try it in two minutes

You need Python 3.11 or later and PostgreSQL 17 or 18 (on a Mac: `brew install postgresql@17`).

```bash
bash examples/local_demo.sh
```

It builds a small database (S), copies it as the old copy (B), changes 1% of the rows on S, then:
1. copies all of S with `pg_basebackup`, for comparison;
2. refreshes B with pgdelta, sending only the chunks that differ;
3. starts the refreshed copy and checks that its rows match S.

On a laptop it prints something like:

```
S and its old copy B: 579M each
changed 1% of 2000000 rows (hot)
1. pg_basebackup copied all of S: 341M in 0.51 s
2. pgdelta refreshes B into O:
pgdelta: published …/demo-work/O (reuse mode): 982 files verified, 798 pages received, 42893 pages reused from the basis
pgdelta: complete (reuse mode: source and basis identities match): 798 of 43691 pages sent; 6596008 bytes to the destination, 63107 bytes back; 0.414 s
3. O started: rows and balance 2000000|140000, source 2000000|140000
   MATCH
```

`CHANGE=uniform` updates every 100th row instead, which touches nearly every page. A laptop has no network, so judge pgdelta here by the bytes it sends and by the copy being right; time is what the cloud benchmarks measure.

## How it works

1. **What to compare.** Both sides split their copy into 8 KiB pages and group the pages into nodes (a hash tree). The sender hashes S; the receiver hashes B. A node whose digest matches is skipped whole; only nodes that differ are compared page by page.
2. **No waiting.** Several files are hashed ahead of the one being sent (`--hash-ahead`, `--window`), on 4 worker processes a side (`--jobs`), so the questions and answers overlap the hashing instead of waiting for it.
3. **What to send.** Only pages that differ, plus whatever B lacks. A stopped source is read directly (`--source-kind datadir`) and, like `pg_basebackup`, old WAL is left out: the copy gets only the WAL it needs to start. A running source is read in backup mode (`--source-kind live`).
4. **How the copy is built.** Either a new copy O beside B, verified by reading it back and published by one rename, or B patched in place (`--in-place`): only the changed pages are written, each checked as it is written, with the files' syncs on a worker thread; B stays blocked from starting until every file matches.

See [pgdelta/README.md](pgdelta/README.md) for every option.

## Where it stands

Same-session race on two `n2-standard-4` VMs in two zones of us-central1, link capped at 1 Gbit/s, pgbench scale 20, every lane timed on the VM, two runs each, every copy started and checked:

| Time vs `pg_basebackup` | Hot change (1% of rows, one range) | Uniform change (every 100th row) |
|---|---|---|
| pgdelta, in place | **0.55×** (2.85 s, 23 MB sent) | 1.12× (6.17 s, 229 MB) |
| pgdelta, new copy | 0.92× (4.76 s, 23 MB) | 1.12× (6.17 s, 229 MB) |
| the November rebuild (#44), new copy | 1.74× (9.02 s, 293 MB) | 1.65× (9.08 s, 498 MB) |
| `pg_basebackup` | 1.00× (5.18 s, 341 MB) | 1.00× (5.50 s, 340 MB) |

## Who built what

| Feature | Built by |
|---|---|
| Comparing S and B by keyed digests; the verified, atomically published copy | Matthew Wei |
| Pipelined digest exchange: many pages per question, both sides hashing at once, no per-page round trips | Matthew Wei (phase 1); rebuilt independently by Sam Ethan Mathew (PRs #43, #44) |
| Hash tree (one digest per node of pages) | Matthew Wei |
| Hashing on worker processes | Matthew Wei |
| Leaving out old WAL; stopped and running sources | Matthew Wei |
| In-place refresh, verify as written, syncs on a worker | Matthew Wei |
| Finding that the destination's disk limits a pipelined refresh | Matthew Wei (phase 1, EXP-92); confirmed independently by Sam Ethan Mathew (#36) |
| Live progress events, the viewer (`viz/`) and benchmark runner (`bench/`) | Matthew Wei |
| Benchmark harness (`benchmarks/`) | Sam Ethan Mathew, Matthew Wei, Etash Jhanji |

To see it, run `python -m viz race`: a live laptop race against `pg_basebackup` in your browser (see [viz/README.md](viz/README.md)).

## The roadmap

The [open issues](https://github.com/MattyWeee123/pg-delta-backup/issues) labelled `track` are the plan, one owner each. With the first phase's engine back on `main`, much of what they describe (pipelining, in-place refresh, leaving out old WAL) already exists; what is left is measuring it where it matters (a database bigger than memory, slow links), the correctness campaign on the restored engine, and the demo. Pick one, open a branch, and send a pull request.

## Repository layout

```text
pgdelta/              the tool: protocol, sender, receiver, hash tree, in-place refresh, command line
tests/                unit tests: python -m unittest discover -s tests
examples/             local_demo.sh, the two-minute quick start
viz/                  watch a run live or replay one in a browser, and launch new runs: python -m viz serve
bench/                run a benchmark (laptop or Google Cloud), record it, publish it as a PR: python -m bench run
benchmarks/           the team's measurement harness: pg_basebackup, native incremental, rsync and pgdelta,
                      a Docker fixture, HammerDB load, and backup verification
infra/gcp/            create, throttle and delete benchmark VMs on Google Cloud
experiments/network/  VM helpers: remote control (remote.py, agent.py), the 0.1-second sampler of link,
                      disk and CPU (util_series.py), the hash benchmark (hashbench.py)
docs/                 research summary, past results, correctness notes
```

## Working on it

- **One branch per issue,** and a pull request with the tests passing. The pull request template asks for the evidence.
- **Measure before and after.** An improvement counts when a benchmark shows it, on the same setup as the run it improves on. `python -m bench run --publish` (or the launcher at `python -m viz serve` → **New run**) records a run and opens a pull request with its report; see [bench/README.md](bench/README.md).
- **This repository is public.** Never commit IP addresses, email addresses, billing IDs, keys or tokens.

Some older documents (in `benchmarks/`, `infra/` and `docs/`) link to files that now live only in the archive.
