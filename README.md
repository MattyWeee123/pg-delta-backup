# pg-delta-backup

A CMU student project with Google's AlloyDB team: a tool that brings an older physical copy of a PostgreSQL cluster up to date by sending only the pages that changed, verifying every reused page by hash.

**Status, October 23, 2026** (details in the [project log](docs/LOG.md))
- **Tool: v0 built** ([`pgdelta/`](pgdelta/README.md), [design](docs/DESIGN.md)).
  - It compares 8 KiB pages by keyed hash, sends only the ones that differ, and rebuilds and verifies a new copy before publishing it.
  - It hashes on worker processes (`--jobs`, default 4), building the new copy by copy-then-patch and verifying it in parallel.
- **Evidence** ([all experiments](docs/experiments/README.md), each with predictions recorded before running):
  - **Correct on ten tricky old copies,** including after failover, where skipping pages by LSN would have kept stale data ([EXP-03](docs/experiments/EXP-03-tricky-copies.md)). All 756 runs between two machines produced copies that passed `pg_verifybackup`, and every pgdelta and rsync copy was byte-identical to its source backup.
  - **Sends exactly the changed pages,** about 1% of the database for local changes ([EXP-01](docs/experiments/EXP-01-change-patterns.md), [EXP-02](docs/experiments/EXP-02-local-comparison.md)). On the wire, PostgreSQL's incremental backup sends slightly less, because staged methods must send a padded WAL segment.
  - **Worker processes cut its fixed cost by 2–3×** ([EXP-06](docs/experiments/EXP-06-engine-throughput.md)); threads didn't, because of Python's interpreter lock ([EXP-05](docs/experiments/EXP-05-parallel-hashing.md)).
  - **Between two cloud VMs** ([EXP-04](docs/experiments/EXP-04-network-sweep.md), [EXP-04b](docs/experiments/EXP-04b-default-checksums.md)), for 2.1 GB with 1% of rows changed:
    - PostgreSQL's incremental backup is the fastest delta method wherever it can be used. A full copy beats it only on the fastest links.
    - Where it can't, pgdelta is fastest only for scattered changes below about 2 Gbit/s, and rsync for local changes below about 3.9 Gbit/s.
    - A full copy wins on faster links.
  - **Why staged pgdelta can't win on fast links:** the local backup it starts from costs as much as a full copy ([EXP-07](docs/experiments/EXP-07-capture-speed.md)), and its own transfer is about as long again.
- **Target and decisions (October 23):** AlloyDB Omni's HA standby rebuild and DR re-seed. The old copy may be patched in place, TLS on the wire, no GPL code ([decisions](docs/DECISIONS.md#decisions-of-october-23-2026-t1-closed), [what Omni does today](docs/research/notes/alloydb-omni.md)). No sponsor questions are blocking.
- **Next:** compare against the incumbent tools (T11), read the source's files directly instead of taking a backup first (T8a), and fix the destination write path (T3). Each has its predictions recorded before it runs.
- **Measurement harness: built.** Backup and restore checks, resource sampling, the HammerDB slowdown driver, and a two-VM network benchmark ([experiments/network/](experiments/network/README.md)).
- **Research: done.** [What already exists and where the gap is](docs/research/state-of-the-art.md).

![Who wins where](docs/figures/exp04b-winners.svg)

## The problem and our niche

A destination already holds an older copy of a database cluster. `pg_basebackup` ignores it and re-sends everything.

Existing tools reuse the old copy only while a shared history holds:
- PostgreSQL 17+ incremental backup needs the old backup's manifest and unbroken WAL summaries.
- `pg_rewind` needs the old copy's WAL back to a failover.
- `pg_probackup catchup` and WAL-G catchup trust page LSNs and file times on the same timeline.
- pgBackRest verifies by hash, but only restores from its own repository.

Our niche is the copy whose history is lost: a standby whose WAL was recycled, an ex-primary after failover, a restored disk snapshot. We refresh it straight from the live server, verify every reused page by hash, and need no repository, manifest or shared timeline. Where PostgreSQL's own tools apply, we hand off to them.

**What we don't claim:** to be a backup system, to read less than native incremental when its prerequisites hold, or to beat a parallel full copy on a very fast LAN. The benchmark reports the link speed where the tool starts to win.

## Start here

1. [Research summary](docs/research/state-of-the-art.md): existing tools, correctness pitfalls, design implications.
2. [Experiments](docs/experiments/README.md): each one's question, predictions made before running, results and graphs.
   - [Decision ledger](docs/DECISIONS.md): every design decision graded by its evidence, and the experiments that would settle the gray areas.
3. [Design of pgdelta v0](docs/DESIGN.md) and [how to run it](pgdelta/README.md).
4. [Project log](docs/LOG.md): what was done, when, and why.
5. [Plan](docs/PLAN.md): workstreams, dependencies, schedule to the November 13 freeze.
6. [Specification](docs/SPEC.md): architecture, design decisions, correctness gates.
7. [Correctness contract](docs/CORRECTNESS.md): what "no data loss" means and how we test it.
8. [Questions for Ben and Sumaithri](docs/BEN-QUESTIONS.md): decisions taken, our assumptions, and non-blocking questions.
9. [References](docs/REFERENCES.md): curated reading list.
10. [Benchmark harness](benchmarks/README.md) and [native recovery suite](benchmarks/NATIVE.md).
11. [Validation log](docs/VALIDATION.md): what has actually been run.

Work items are GitHub issues #4 to #13. Older planning docs are in [docs/archive](docs/archive/).

## Run the tests and the benchmark runner

Python 3.10+:

```sh
python -m unittest discover -s tests -v
```

`benchmarks/benchmark.py` runs one method from the [method registry](benchmarks/methods.py): a full backup, native incremental plus reconstruction, or a staged rsync or pgdelta transfer from a fresh backup into a copy of an old one. It verifies the output with `pg_verifybackup` and writes a shared v3 JSON run record. It needs PostgreSQL 17 client utilities and a disposable test cluster; see [the benchmark plan](benchmarks/README.md#running-the-consolidated-cli) for the fixture and flags.

```sh
python benchmarks/benchmark.py --host 127.0.0.1 --port 5432 --user postgres --out benchmarks/runs
```

A successful run records `PASS_MANIFEST_ONLY`, not `PASS`: this runner has no restore gate. `network_bytes` is null for the native methods until network measurement exists; the staged methods fill it from their own transfer counters.
The native suite performs real restores; HammerDB supplies the larger workload.

`experiments/chunk_demo.py` is a toy model of the engine on synthetic bytes. It doesn't touch PostgreSQL or a network.
`experiments/change_patterns/` measures how many pages really change between two backups under different row-change patterns, and how that compares with PostgreSQL's own change tracker ([README](experiments/change_patterns/README.md)).

## Repository layout

```text
pgdelta/         The tool (v0): hash tree, protocol, sender, receiver
docs/            Plan, spec, design, log, correctness contract, sponsor questions, references
docs/experiments/ Experiment write-ups, curated data (data/) and figures (../figures/)
docs/research/   State-of-the-art research and its source notes
docs/references/ Open-licensed papers (others are linked in REFERENCES.md)
docs/specs/      Design and handoff records for the measurement harness
docs/archive/    Superseded planning documents
benchmarks/      Measurement harness, fixture, workloads and recorded evidence
experiments/     Tooling for each experiment (change patterns, tricky copies, network)
analysis/        Scripts that turn raw results into the committed data and figures
infra/gcp/       Create, throttle and delete the two benchmark VMs
tests/           Unit and contract tests
.github/         CI and issue/PR templates
```

## Language

v0 is Python: it plugs into the existing harness, and hashing runs as compiled code through `hashlib`.
EXP-02 showed hashing is the bottleneck. EXP-05 tests whether hashing on several threads is enough
before reaching for a compiled core (Rust).
C would only be needed for a PostgreSQL patch, which is out of scope.

## Contribution rules

Every issue has one owner, a reviewer, an artifact, and a falsifiable completion criterion.
Research ends in a short decision note, an experiment, or a PR, not only a reading list.
Do not use real customer data, commit credentials, or treat a successful hash check as proof of database recovery.
No open-source license is selected until the collaboration's ownership and contribution terms are confirmed.
