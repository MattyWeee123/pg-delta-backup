# pg-delta-backup

Research starter for the CMU / AlloyDB Omni collaboration.
Status: sponsor direction clarified by supplied Google Chat screenshots; implementation design remains proposed.
This repository does not yet contain a PostgreSQL backup implementation.

## The problem

A destination already has an older physical copy of a database cluster.
Can we produce a correct new backup while sending substantially fewer bytes across the constrained network link by reusing matching data already there?

The sponsor expects a benchmark under active database workload, comparing backup time, network traffic and application performance degradation across team-defined workload scales.
The output must preserve committed data through a defined recovery point with appropriate backup/recovery guarantees.
It need not be byte-for-byte identical to an independently captured pg_basebackup output.
Operation without old WAL summaries remains a possible differentiating experiment, not the sponsor's established central requirement.
PostgreSQL 17+ incremental backup, rsync, and pgBackRest are relevant existing solutions.
We must establish when our approach is useful relative to them.

## Start here

1. [Lead briefing](docs/LEAD-BRIEF.md): understand the problem and prepare for the next sync.
2. [Questions for Ben](docs/BEN-QUESTIONS.md): resolve the architecture-changing assumptions.
3. [Specification](docs/SPEC.md): scope, interfaces, safety properties, and acceptance criteria.
4. [Plan and assignments](docs/PLAN.md): first 48 hours, first week, and milestones.
5. [Research and code map](docs/RESEARCH.md): verified sources and reading tasks.
6. [Benchmark and restore plan](benchmarks/README.md): measure equivalent outcomes.
7. [Issue backlog](docs/BACKLOG.md): ready-to-use work items.
8. [Correctness and recovery contract](docs/CORRECTNESS.md): proposed measurable meaning of no data loss.

## Recommended first implementation

Use completed, immutable plain-format `pg_basebackup` outputs as inputs to a delta-transfer experiment.
Capture the new backup on the source side of the constrained network link.
Reconstruct into a new destination directory using old destination chunks plus newly transmitted bytes.
Then verify and test-restore the reconstructed backup.

This first architecture saves transfer bytes on that link, but still makes a full source-side backup and reads/hashes data.
It is not a claim of lower source I/O or a finished live-backup replacement.
Use this as an internal integration milestone on the path to the required active-workload benchmark.
The team owns its milestones; source-helper permissions and final deployment constraints still need clarification.

## Run the educational experiment

Python 3.10+; no third-party dependencies, database, or network required:

```sh
python experiments/chunk_demo.py
python -m unittest discover -s tests -v
```

The demo compares whole-file transfer with fixed-size chunk reuse on deterministic synthetic bytes.
It proves byte reconstruction for its fixtures only.
It does not benchmark PostgreSQL, network transfer, live consistency, or durable backup publication.

## Run the benchmark runner

`benchmarks/benchmark.py` is the measurement harness: it runs one backup method, times it, verifies
the output with `pg_verifybackup`, and writes a JSON run record.
It needs PostgreSQL 17 client utilities and a disposable test cluster; see
[the benchmark plan](benchmarks/README.md#running-the-v01-runner) for the fixture and flags.

```sh
python benchmarks/benchmark.py --host 127.0.0.1 --port 5432 --user postgres --out benchmarks/runs
```

A successful run records `PASS_MANIFEST_ONLY`, not `PASS`: the runner has no restore gate, so
`restore_tested` is always false and `network_bytes` is always null.
Workload generation, native incremental backups, and the custom delta method are not implemented.

## Repository layout

```text
docs/          Briefing, specification, decisions, plan, issue backlog
experiments/   Small reproducible research experiments
tests/         Executable checks for the experiments
benchmarks/    Methodology, the measurement runner, and a disposable cluster fixture
.github/       CI and issue/PR templates
```

Keep the implementation in this repository as modules with shared tests.
Create a separate PostgreSQL fork only if Ben requires a backend patch.
Recommended production core: Rust if a standalone helper is accepted; C if upstream PostgreSQL integration is required.
Python is used here only for the disposable research experiment and future orchestration.
The team has not yet selected a language; confirm whether upstream integration imposes a constraint.

## Contribution rules

Every issue has one owner, a reviewer, an artifact, and a falsifiable completion criterion.
Research ends in a short decision note, an experiment, or a PR, not only a reading list.
Do not use real customer data, commit credentials, or treat a successful hash check as proof of database recovery.
No open-source license is selected until the collaboration's ownership and contribution terms are confirmed.

## Validation status

See [local validation](docs/VALIDATION.md) for what was actually run.
PostgreSQL/AlloyDB integration and performance results are not yet established.
