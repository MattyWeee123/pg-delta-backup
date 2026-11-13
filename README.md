# pg-delta-backup

A CMU student project with Google's AlloyDB team: a tool that brings an old copy of a PostgreSQL database up to date by sending only the parts that changed, instead of copying everything again the way `pg_basebackup` does.

**Fresh start, November 13, 2026.** `main` now begins from a deliberately naive version, so the team can build each improvement step by step and measure what it buys. Everything from the first phase (591 commits: the full tool, 93 experiments, the research and the results) is preserved on branch [`archive/v1-research`](https://github.com/MattyWeee123/pg-delta-backup/tree/archive/v1-research) (tag `v1-research-final`). [docs/PAST-RESULTS.md](docs/PAST-RESULTS.md) summarizes what it found.

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
1. pg_basebackup copied all of S: 341M in 0.57 s
2. pgdelta refreshes B into O:
pgdelta: 998 files, 67471 chunks unchanged, 438 changed, 384 new; sent 9,089,083 bytes (1.62% of 559,352,788 bytes); 67909 round trips; 3.91 s; every file verified
3. O started: rows and balance 2000000|140000, source 2000000|140000
   MATCH
```

`CHANGE=uniform` updates every 100th row instead, which touches nearly every page. A laptop has no slow network, so judge pgdelta here by the bytes it sends and by the copy being right, not by time. Time is what the benchmarks on Google Cloud measure.

## How the naive version works

For every 8 KiB chunk of every file, the sender hashes its chunk and asks the receiver "is yours the same?". It waits for the answer, then sends the chunk only if it differs. One thread, one TCP connection, one round trip per chunk. The receiver builds the new copy beside the old one, verifies every file against the source's digest, and publishes it with one rename. See [pgdelta/README.md](pgdelta/README.md).

It is slow on purpose: every wait in it is a step on the roadmap.

## The roadmap

The [open issues](https://github.com/MattyWeee123/pg-delta-backup/issues) are the plan, in order. Each is one improvement and its benchmark: pipelining, then parallelism, then the tooling that makes benchmarking easy. Pick one, open a branch, and send a pull request.

## Repository layout

```text
pgdelta/              the tool (naive v0): protocol, sender, receiver, command line
tests/                unit tests: python -m unittest discover -s tests
examples/             local_demo.sh, the two-minute quick start
benchmarks/           the team's measurement harness: pg_basebackup, native incremental, rsync and pgdelta,
                      a Docker fixture, HammerDB load, and backup verification
infra/gcp/            create, throttle and delete benchmark VMs on Google Cloud
experiments/network/  VM helpers: remote control (remote.py, agent.py), the 0.1-second sampler of link,
                      disk and CPU (util_series.py), the hash benchmark (hashbench.py)
docs/                 research summary, past results, correctness notes
```

## Working on it

- **One branch per issue,** and a pull request with the tests passing. The pull request template asks for the evidence.
- **Measure before and after.** An improvement counts when a benchmark shows it, on the same setup as the run it improves on.
- **This repository is public.** Never commit IP addresses, email addresses, billing IDs, keys or tokens.

Some older documents (in `benchmarks/`, `infra/` and `docs/`) link to files that now live only in the archive.
