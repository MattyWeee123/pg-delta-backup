# Cloud backup resource results, November 20, 2026

The tested engine is main at `da1a770f618b1c89d2195357f7b57e3ef9cc3d2e`. Two n2-standard-4 VMs in us-central1-a and us-central1-b used PostgreSQL 17.12 and 50 GB balanced data disks, with a 1 Gbit/s link cap and 0.81 ms RTT. Pgbench scale 50 contains 5 million accounts. The hot workload updated the first 1% of account IDs. Each method ran three times, with caches dropped before each lane.

| Median | Full basebackup | Pgdelta, new copy | Pgdelta, in place |
|---|---:|---:|---:|
| Backup time | 11.94 s | 10.06 s | 5.73 s |
| Source interface TX | 817.91 MB | 33.17 MB | 33.20 MB |
| Destination disk writes, setup plus backup | 833.86 MB | 863.27 MB | 38.89 MB |
| Combined CPU work, setup plus backup | 5.66 core-s | 14.39 core-s | 10.19 core-s |
| Source peak backup-process RSS | 26.0 MiB | 142.3 MiB | 142.4 MiB |
| Destination peak backup-process RSS | 12.0 MiB | 133.0 MiB | 130.3 MiB |

CPU work in the table is the median of each trial’s combined source and destination CPU seconds. Setup-inclusive observations capture receiver hashing before the sender timer starts, together with SSH/control setup overhead. Trial reset copies made before sampling are excluded. Elapsed times use the existing runner’s command timers. Network totals above cover those timed intervals and include interface traffic such as SSH and protocol overhead. MB are decimal. MiB use powers of two.

The in-place mode took 52% less time, sent about 96% fewer bytes, and wrote about 95% fewer destination bytes in this workload. It used more CPU and sampled process memory. These measurements cover this hot workload, size and VM configuration.

## Measurement and correctness

The existing cloud runner and VM CPU/disk/network sampler are Matthew Wei’s. Process-resource helpers are credited to Etash Jhanji. A run-specific memory extension and analysis were prepared with Codex using those helpers. The engine was unchanged. The sampler read Linux counters every 100 ms. Summed RSS counts resident pages across matching processes and can count shared pages multiple times. Sampling can miss brief peaks. Monitoring overhead was not separately quantified.

All nine copies started and matched the source account count and balance sum. Full backups additionally passed pg_verifybackup. Pgdelta performed its built-in file verification. Full-table ordered exports were outside these cloud checks.

## Evidence

- [Full resource report](resources.md) includes timed and setup-inclusive counters.
- [Backup timing and protocol report](report.md) lists every lane and copy check.
- [Per-trial resource results](resources.json) preserve the aggregated measurements.
- [Configuration](config.json), [run metadata](meta.json), and [steps](steps.json) record the setup and timing boundaries.
- [Raw VM samples](raw/) contain the original counter streams and per-lane start/end timestamps.
- The stats-pgdelta JSON files hold engine counters and verification receipts. Events and charts support replay.
- [Resource provenance](resource-provenance.json) records the memory sampler checksum and engine revision.
- [Cost observation](cost-observation.json) records the estimate and cleanup verification.

Provisioning through deletion took 9.04 minutes. Estimated compute/storage and inter-zone transfer total approximately $0.094, before small incidental charges. This is an estimate, with billing unverified. Both VMs and all associated disks were verified deleted.

## Reproduce the analysis

From the repository root, run the following against the archived samples. This needs no cloud resources.

```bash
python3 benchmarks/evidence/2026-11-20-cloud-resources-hot/instrumentation/analyze.py benchmarks/evidence/2026-11-20-cloud-resources-hot
```

The script regenerates resources.json and the measurement tables in resources.md. This README retains the configuration and interpretation.

## Repeat the cloud experiment

Use the pinned engine revision and the archived instrumentation with Python 3 and authenticated gcloud access. The cloud wrapper runs the existing bench runner with a measurement-only sampler extension and adds full-backup restore checks. It has a 20-minute timeout and uses the runner’s VM cleanup. It requires an explicit project. On a checkout with an existing run of the same name, preserve or relocate that run before repeating.

```bash
python3 benchmarks/evidence/2026-11-20-cloud-resources-hot/instrumentation/prepare.py
python3 benchmarks/evidence/2026-11-20-cloud-resources-hot/instrumentation/run.py --project YOUR_PROJECT
```

The archived sampler is identical to the measured sampler. The archived wrapper uses a portable repository path and requires explicit project selection.
