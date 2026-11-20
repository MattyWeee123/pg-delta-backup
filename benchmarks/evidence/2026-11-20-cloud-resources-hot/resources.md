# Cloud resource measurements

Medians across the recorded repetitions. CPU, disk and network are VM/device counters inside the backup interval. Counter values at interval boundaries are linearly interpolated between 100 ms samples. Memory peaks are sampled. Process RSS sums can count shared pages multiple times. Copy verification after the backup timer is excluded.

| Method | Runs | Time (s) | Source CPU (core-s) | Destination CPU (core-s) | Source CPU (%) | Destination CPU (%) |
|---|---:|---:|---:|---:|---:|---:|
| pg_basebackup | 3 | 11.94 | 2.81 | 2.02 | 5.85 | 4.23 |
| pgdelta | 3 | 10.06 | 4.91 | 6.84 | 12.05 | 16.92 |
| pgdelta-inplace | 3 | 5.73 | 4.47 | 3.63 | 19.06 | 15.73 |

| Method | Source peak process RSS (MiB) | Destination peak process RSS (MiB) | Source disk read (MB) | Source disk write (MB) | Destination disk read (MB) | Destination disk write (MB) |
|---|---:|---:|---:|---:|---:|---:|
| pg_basebackup | 26.0 | 12.0 | 815.2 | 34.0 | 0.0 | 833.9 |
| pgdelta | 142.3 | 133.0 | 832.7 | 0.1 | 106.2 | 863.2 |
| pgdelta-inplace | 142.4 | 130.3 | 832.7 | 0.1 | 120.5 | 38.8 |

| Method | Source → destination interface TX (MB) | Destination → source interface TX (MB) |
|---|---:|---:|
| pg_basebackup | 817.91 | 1.67 |
| pgdelta | 33.17 | 0.16 |
| pgdelta-inplace | 33.20 | 0.18 |

## Observed setup plus backup

This view starts when both samplers are available and ends at the backup end. It includes receiver work before sender T0 and SSH/control setup overhead. Trial reset copies made before sampling are excluded.

| Method | Source CPU (core-s) | Destination CPU (core-s) | Source disk read (MB) | Destination disk read (MB) | Destination disk write (MB) |
|---|---:|---:|---:|---:|---:|
| pg_basebackup | 3.22 | 2.44 | 815.62 | 0.01 | 833.86 |
| pgdelta | 5.82 | 8.22 | 832.68 | 808.20 | 863.27 |
| pgdelta-inplace | 5.27 | 4.94 | 832.68 | 807.85 | 38.89 |

Network counters include all traffic on the measured interfaces, including SSH control traffic. Use engine protocol counters alongside them. Whole-VM CPU also includes operating-system work. The sampler extension and its checksum are recorded separately from the engine revision.

## Configuration and correctness

Current main engine da1a770f618b1c89d2195357f7b57e3ef9cc3d2e, with a measurement-only sampler extension for memory. Two n2-standard-4 VMs (4 vCPUs and 16 GB RAM each), 50 GB balanced data disks, PostgreSQL 17.12, 1 Gbit/s link cap, 0.81 ms RTT. Pgbench scale 50, 5 million accounts, 1% hot updates, three repeats per method, caches dropped before every lane. The stopped source had about 1.5 GB on disk including WAL; full backup transferred about 816 MB. These results cover the hot-update workload only.

All nine outputs started and matched the source account count and balance sum. All three full backups additionally passed pg_verifybackup; pgdelta performed its built-in file verification for its six outputs. The engine was unchanged. These cloud restore checks compare count and balance, and do not perform the earlier laptop suite’s ordered exports of every table.

## Cost and cleanup

Both owned VMs and all their disks were verified absent after the run. Provisioning through deletion took 9.04 minutes. The runner’s recorded hourly rates give approximately $0.061 for compute/storage. Measured backup traffic plus an estimate for the initial basis add approximately $0.032 at the published [inter-zone rate](https://cloud.google.com/vpc/network-pricing). Combined estimate is about $0.09. This is a runtime-based estimate, not a billing invoice, and excludes small external-IP/control-traffic charges.
