# Benchmark runner v0.1 design

Date: October 9, 2026.
Status: historical v0.1 design from Sam’s branch. The implemented successor is
[the consolidated v2 runner](../../benchmarks/CONSOLIDATION.md). The ten-field
record, CRC32C default and original validation limits below describe that earlier
checkpoint; current code adds incremental reconstruction and shares its adapters
with the native recovery suite.
Scope: the measurement harness only. No delta-backup algorithm is designed or implemented here.

## Why this exists first

`docs/BACKLOG.md` item 07 ("Build fair measurement harness") is P0 and owned by the
workloads/measurement role. `docs/PLAN.md` "First 48 hours" item 4 directs that owner to start
baselines in parallel with engine work. A reusable measurement path must therefore exist before
the delta executable does, so that the custom method is compared against a baseline produced by
identical instrumentation rather than a separately written script.

`benchmarks/README.md` already specifies the methodology. This runner implements the narrowest
useful slice of it and nothing more.

## What v0.1 does

1. Runs one PostgreSQL backup method (`pg_basebackup`, full, plain format, streamed WAL).
2. Measures wall-clock duration and resulting on-disk byte count.
3. Verifies the output with `pg_verifybackup` against its manifest.
4. Writes one JSON run record plus logs into a unique per-run directory.

## What v0.1 explicitly does not do

- No restore test. `docs/SPEC.md` section 7 and the PostgreSQL documentation both state that
  `pg_verifybackup` does not replace a test restore. The recorded status is therefore
  `PASS_MANIFEST_ONLY` and `restore_tested` is always `false`. Gate G3 remains unmet.
- No network measurement. `network_bytes` is always `null`. A single-host run has no measured
  link, and `benchmarks/README.md` forbids writing zero into an unavailable field.
- No workload generation. pgbench and HammerDB are v0.2.
- No incremental backup, no `pg_combinebackup`, no custom delta method. Those register against
  the method seam described below when they exist.
- No concurrent orchestration, so no application-degradation metrics.

## Layout

```text
benchmarks/
  README.md              methodology, unchanged except for a new runner section
  benchmark.py           the runner
  fixture/
    docker-compose.yml   disposable PostgreSQL 17 cluster
    pg_hba.conf          replication access for the fixture
  runs/<run-id>/         gitignored
    backup/
    pg_basebackup.log
    pg_verifybackup.log
    versions.txt
    result.json
```

The runner lives inside the existing `benchmarks/` directory rather than a sibling
`backup-benchmark/`, so that the methodology document and the code implementing it stay together
and there are not two near-identically named top-level directories.

## Run record

Exactly ten keys, fixed by `RESULT_KEYS` and asserted by a test so the schema cannot drift
unnoticed:

```text
run_id  method  backup  verification  backup_file_bytes
time_to_verified_seconds  integrity_verified  restore_tested  network_bytes  status
```

`backup` and `verification` are each `{exit_code, duration_seconds, error}`. `verification` is
`null` when the backup failed and verification was therefore not attempted.
`time_to_verified_seconds` is `null` unless the run reached `PASS_MANIFEST_ONLY`, because time to
a verified backup is meaningless when nothing was verified.

This record deliberately omits the richer fields listed in `benchmarks/README.md` "Results
record" (versions, git commit, measurement kind, units, traffic by direction, CPU, memory). Those
belong to later versions. To avoid losing the provenance the methodology requires, the facts that
have nowhere to live in the JSON are written to `versions.txt` in the same run directory: tool
versions, git commit, platform, manifest checksum algorithm, backup format, checkpoint mode, and
whether `PGPASSWORD` was set in the environment. The full command line is written as the first
line of each log.

## Status and exit codes

| Status | Condition | Exit code |
|---|---|---|
| `PASS_MANIFEST_ONLY` | backup and verification both succeeded | 0 |
| `FAIL_BACKUP` | backup returned nonzero, timed out, or could not start | 3 |
| `FAIL_VERIFY` | backup succeeded, verification did not | 4 |
| (no record written) | unsupported or unsafe input detected before running | 2 |

Exit codes reuse the contract already defined for the future CLI in `docs/SPEC.md` section 3
(0 success, 2 invalid/unsupported input, 3 I/O failure, 4 verification failure) rather than
inventing a parallel vocabulary.

## Method seam

A module-level `METHODS` dict maps a method name to a function returning the command list:

```python
METHODS = {'pg_basebackup_full': pg_basebackup_full}
```

The dict key is both the `--method` argument and the value recorded in the JSON `method` field.
Adding native incremental, `pg_combinebackup`, or the custom delta executable is one function and
one dict entry. This matches the plain-function idiom already used in `experiments/chunk_demo.py`
and avoids a class hierarchy whose shared behaviour is still guesswork at one implementation.

## Safety properties

- No password is ever placed in `argv`. The runner passes `--no-password` so `pg_basebackup`
  fails fast instead of blocking on an interactive prompt, and relies on `.pgpass` or
  `PGPASSFILE`. `versions.txt` records whether `PGPASSWORD` was set, never its value.
- `--source-pgdata` is optional; when given, the runner refuses a backup destination that equals,
  contains, or is contained by the source data directory. This is the footgun
  `benchmarks/README.md` warns about. The flag is named `--source-pgdata` to avoid confusion with
  `pg_basebackup --pgdata`, which is the destination.
- A non-empty backup destination is refused, per `docs/SPEC.md` section 4.
- Missing `pg_basebackup` or `pg_verifybackup` fails with exit 2 before anything runs, rather
  than producing a half-populated run directory.
- Verification is skipped when the backup failed, so a confusing second log is not produced.
- `--checkpoint` defaults to `fast`. PostgreSQL's own default is `spread`, whose server-paced
  delay would add uncontrolled variance to a timed measurement. The choice is recorded in
  `versions.txt` because it materially changes the measured duration.

## Testing

`tests/test_benchmark.py`, stdlib `unittest`, matching the existing `tests/test_chunk_demo.py`
style. No PostgreSQL is required: command execution is exercised against short Python
subprocesses, and `main()` is exercised end to end against stub executables that imitate
`pg_basebackup` and `pg_verifybackup`. Coverage:

- `run_command`: success, nonzero exit, stderr captured into the log, missing binary, timeout.
- `directory_bytes`: nested tree, symlinks ignored.
- Command builders: required flags present, no password in `argv`.
- `classify`: table-driven over every status and exit-code pair.
- `assert_no_overlap`: equal, nested both directions, sibling.
- `build_result`: exact key set, null handling on each failure path.
- `main`: success, verification failure, backup failure skipping verification, missing tool,
  overlapping paths.

## Validation limits of this change

The runner and its tests are executed and pass on the development host. The Docker fixture is
**not** validated: the Docker daemon was unreachable and no PostgreSQL client tools are installed,
the same condition already recorded in `docs/VALIDATION.md`. The fixture and the first real
`result.json` must be confirmed by whoever first brings a cluster up. No measured PostgreSQL
result is claimed by this change.
