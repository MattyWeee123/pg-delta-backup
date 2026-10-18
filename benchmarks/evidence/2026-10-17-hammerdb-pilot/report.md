# HammerDB TPROC-C degradation pilot, October 17 2026

First automated matched-pair measurement from
`benchmarks/hammerdb_experiment.py`. This validates the instrument. **It is not a
baseline**: one repetition on a shared laptop, with the limits listed at the end.

## Configuration

| | |
|---|---|
| Cluster | TPROC-C, 10 warehouses, 1,231 MB at run start |
| Workload | 8 virtual users, 1 minute ramp-up, 11 minute measurement |
| Backup | `pg_basebackup_full`, plain format, streamed WAL, SHA256 manifest, fast checkpoint |
| Trial order | `backup` then `control`, drawn from seed 116458667 |
| Host | Single laptop; database, workload and backup share CPUs and one SSD |

## Result

The backup ran for 328.4 s of the 674.5 s measurement window.

| Window | Control | Backup | |
|---|---|---|---|
| Whole measurement | 20,093.4 tpm (n=67) | 8,528.7 tpm (n=68) | 57.6% apparent loss |
| **Aligned analysis** | **18,979.3 tpm (n=33)** | **5,432.6 tpm (n=32)** | **71.4% loss** |
| Recovery, after capture | 18,979.3 reference | 11,280.8 tpm (n=36) | 40.6% still down |

Both aligned windows span exactly 328.388 s at the same offset from their own ramp-up.

```text
throughput_loss  = 1 - 5432.6 / 18979.3 = 0.7138
latency_increase = 153.987 / 57.859 - 1  = 1.6614   (whole run, not window aligned)
```

Backup: `PASS_MANIFEST_ONLY`, capture 232.7 s, verification 95.6 s, output
2,015,067,325 bytes measured inside the container. Aggregate NOPM was 8,930 for the
control and 3,939 during the backup.

## Why the aligned figure is the one to read

The whole-window comparison gives 57.6%, the aligned comparison 71.4%. The gap is
not noise: 346 s of the 674 s window had no backup running, so the whole-window
average dilutes the effect by mixing backup and non-backup time. The same artefact
produced the October 9 figures of 34% and 65% from the same backup on the same
cluster, differing only in when the backup started.

The recovery window is the second thing the aggregate hides. Throughput was still
40.6% below the control after capture and verification finished, across 36 samples.

## Negative evidence

Two earlier runs are retained in `rejected-runs.zip`. Both were **rejected by the
window guard**, correctly:

| Run | Backup span | Window | Outcome |
|---|---|---|---|
| 2 minute window | 174.0 s | 123.8 s | rejected |
| 6 minute window | 398.5 s | 363.7 s | rejected |

A backup that outlives its measurement window cannot be compared against a control,
so no number is reported for it. This mirrors how the native suite rejects a trial
whose backup outlives its workload.

One of those runs also quantifies observer interference: the 6 minute run measured a
control of 8,708 tpm, while an otherwise identical run with a test suite executing
concurrently measured 4,318 tpm. **Unrelated load on the host halved the measurement.**

## What this does not establish

- **One repetition.** `benchmarks/README.md` requires at least three after a warm-up,
  with medians and ranges. No significance is claimed.
- **No resource isolation.** The PostgreSQL server, eight virtual users and the backup
  shared one laptop's CPUs and a single SSD, and the backup wrote to the same disk as
  PGDATA. This measures contention on a shared host.
- **Latency is whole-run.** HammerDB records no time dimension for latency, so the
  166% figure covers each entire run including ramp-up and is not matched to the
  backup window the way throughput is.
- **No network measurement.** `network_bytes` is null with a reason.
- **Nothing was restored.** Status is `PASS_MANIFEST_ONLY`; gate G3 is unmet and no
  TPROC-C recovery oracle exists.
- **TPROC-C is not audited TPC-C.** Never publish these as `tpmC`.

See [BASELINE-GATE.md](../../BASELINE-GATE.md) for what a citable baseline still requires.
