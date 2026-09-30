# pg-delta-backup

Research starter for the CMU / AlloyDB Omni collaboration.
Status: proposed scope and implementation contracts, awaiting sponsor confirmation.
This repository does not yet contain a PostgreSQL backup implementation.

## The problem

A destination already has an older physical copy of a database cluster.
Can we produce a correct new backup while sending substantially fewer bytes across the constrained network link by reusing matching data already there?

Our proposed research question is content-based reuse without requiring WAL summaries covering the entire interval since the old copy.
This is a hypothesis to confirm with Ben, not an established product requirement or a claim of novelty.
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

## Recommended first implementation

Use completed, immutable plain-format `pg_basebackup` outputs as inputs to a delta-transfer experiment.
Capture the new backup on the source side of the constrained network link.
Reconstruct into a new destination directory using old destination chunks plus newly transmitted bytes.
Then verify and test-restore the reconstructed backup.

This first architecture saves transfer bytes on that link, but still makes a full source-side backup and reads/hashes data.
It is not a claim of lower source I/O or a finished live-backup replacement.
Ben must approve this first milestone and the eventual source-side integration model.

## Run the educational experiment

Python 3.10+; no third-party dependencies, database, or network required:

```sh
python experiments/chunk_demo.py
python -m unittest discover -s tests -v
```

The demo compares whole-file transfer with fixed-size chunk reuse on deterministic synthetic bytes.
It proves byte reconstruction for its fixtures only.
It does not benchmark PostgreSQL, network transfer, live consistency, or durable backup publication.

## Repository layout

```text
docs/          Briefing, specification, decisions, plan, issue backlog
experiments/   Small reproducible research experiments
tests/         Executable checks for the experiments
benchmarks/    Baseline and restore methodology
.github/       CI and issue/PR templates
```

Keep the implementation in this repository as modules with shared tests.
Create a separate PostgreSQL fork only if Ben requires a backend patch.
Recommended production core: Rust if a standalone helper is accepted; C if upstream PostgreSQL integration is required.
Python is used here only for the disposable research experiment and future orchestration.
The team and Ben have not yet approved a language.

## Contribution rules

Every issue has one owner, a reviewer, an artifact, and a falsifiable completion criterion.
Research ends in a short decision note, an experiment, or a PR, not only a reading list.
Do not use real customer data, commit credentials, or treat a successful hash check as proof of database recovery.
No open-source license is selected until the collaboration's ownership and contribution terms are confirmed.

## Validation status

See [local validation](docs/VALIDATION.md) for what was actually run.
PostgreSQL/AlloyDB integration and performance results are not yet established.
