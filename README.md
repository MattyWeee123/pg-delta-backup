# pg-delta-backup

A CMU student project with Google's AlloyDB team: a tool that brings an older physical copy of a PostgreSQL cluster up to date by sending only the pages that changed, verifying every reused page by hash.

**Status, October 22, 2026**
- **Measurement harness: built.** Backup and restore checks, resource sampling, and the HammerDB slowdown driver.
- **Tool: not started.** v0 is next ([plan](docs/PLAN.md)).
- **Research: done.** [What already exists and where the gap is](docs/research/state-of-the-art.md).

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
2. [Plan](docs/PLAN.md): workstreams, dependencies, schedule to the November 13 freeze.
3. [Specification](docs/SPEC.md): architecture, design decisions, correctness gates.
4. [Correctness contract](docs/CORRECTNESS.md): what "no data loss" means and how we test it.
5. [Questions for Ben and Sumaithri](docs/BEN-QUESTIONS.md): our assumptions and the few open questions.
6. [References](docs/REFERENCES.md): curated reading list.
7. [Benchmark harness](benchmarks/README.md) and [native recovery suite](benchmarks/NATIVE.md).
8. [Validation log](docs/VALIDATION.md): what has actually been run.

Work items are GitHub issues #4 to #13. Older planning docs are in [docs/archive](docs/archive/).

## Run the tests and the benchmark runner

Python 3.10+:

```sh
python -m unittest discover -s tests -v
```

`benchmarks/benchmark.py` runs a full backup, or native incremental plus reconstruction. It verifies the output with `pg_verifybackup` and writes a shared v2 JSON run record. It needs PostgreSQL 17 client utilities and a disposable test cluster; see [the benchmark plan](benchmarks/README.md#running-the-consolidated-cli) for the fixture and flags.

```sh
python benchmarks/benchmark.py --host 127.0.0.1 --port 5432 --user postgres --out benchmarks/runs
```

A successful run records `PASS_MANIFEST_ONLY`, not `PASS`: this runner has no restore gate, and `network_bytes` is null until network measurement exists.
The native suite performs real restores; HammerDB supplies the larger workload.

`experiments/chunk_demo.py` is a toy model of the engine on synthetic bytes. It doesn't touch PostgreSQL or a network.

## Repository layout

```text
docs/            Plan, spec, correctness contract, sponsor questions, references
docs/research/   State-of-the-art research and its source notes
docs/references/ Open-licensed papers (others are linked in REFERENCES.md)
docs/specs/      Design and handoff records for the measurement harness
docs/archive/    Superseded planning documents
benchmarks/      Measurement harness, fixture, workloads and recorded evidence
experiments/     Toy model of chunk reuse
tests/           Unit and contract tests
.github/         CI and issue/PR templates
```

## Language

Not decided yet; the team confirms at the next sync.
The proposal is Python for v0: it plugs into the existing harness, and hashing runs as compiled code through `hashlib`.
Rust remains an option for the hashing core if measurements show it's the bottleneck.
C would only be needed for a PostgreSQL patch, which is out of scope.

## Contribution rules

Every issue has one owner, a reviewer, an artifact, and a falsifiable completion criterion.
Research ends in a short decision note, an experiment, or a PR, not only a reading list.
Do not use real customer data, commit credentials, or treat a successful hash check as proof of database recovery.
No open-source license is selected until the collaboration's ownership and contribution terms are confirmed.
