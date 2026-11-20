# pgdelta WAL selection results and presentation FAQ

These results describe the November rebuild at `fd3423c` and the tested WAL-selection change at `6f675c4`. Remote `main` subsequently restored the phase-1 engine in `da1a770`, which already selects required WAL through its data-directory mode. Results for that restored engine require a separate benchmark. This document and the tested implementation are preserved on `codex/shutdown-wal-benchmark`.

The update adds opt-in `--wal-mode shutdown`. For supported cleanly stopped snapshots, pgdelta reads PostgreSQL’s control metadata, validates the shutdown checkpoint using `pg_waldump`, and selects its WAL segment for transfer. The original snapshot stays intact. Chunk comparison and whole-file verification remain enabled.

## What hot and uniform mean

Both workloads update **1% of account rows**, adding 7 to each selected account’s balance.

| Workload | Rows updated | Why it matters |
|---|---|---|
| **Hot** | The first contiguous 1% of account IDs | Changes concentrate in a small region and touch relatively few pages. |
| **Uniform** | Every 100th account ID | Changes are spread through the table and touch many more pages. This is a deterministic pattern. |

PostgreSQL stores rows in pages. Updating 1% of rows can change a much larger fraction of pages, along with index pages and WAL.

## Before and after the fix

The controlled pilot used **200,000 accounts**, the same source snapshot and old backup for each variant, and three randomized repeats at each of 100, 500, and 1,000 Mbit/s. The reduction held at all three caps. MB are decimal.

| Workload | Previous pgdelta sent | WAL-selected pgdelta sent | Reduction |
|---|---:|---:|---:|
| Hot | 85.35 MB | 18.08 MB | **78.8%** |
| Uniform | 122.68 MB | 38.60 MB | **68.5%** |

Previously, a missing WAL filename caused pgdelta to send that entire segment. The pilot retained one verified 16 MiB shutdown-checkpoint segment, removing the other WAL segments and auxiliary WAL files from the transfer inventory. The pilot applied this selection through a prepared source view. The committed implementation performs selection inside the sender.

At 100 Mbit/s, including the pilot’s preparation cost, hot refresh time fell from **7.68 to 2.97 seconds**, and uniform from **10.65 to 4.29 seconds**. At 1 Gbit/s, hot was slightly slower including preparation, **1.59 versus 1.63 seconds**. Bandwidth reduction does not guarantee an elapsed-time win at every link speed. All 36 pilot outputs passed restore and data checks. [Paired pilot report](/Users/etashj/Documents/ChatGPT/AlloyDB/benchmarks/shutdown-wal-fd3423c/report.md)

## Larger laptop benchmark

The integrated implementation was tested with **10 million accounts**. The run completed **53 of 72 planned trials** before interruption, and all 53 passed. All hot configurations completed three repeats. Uniform results below are provisional.

Results at **100 Mbit/s** show optimized pgdelta against the other backup methods.

| Workload | Method | Sent | Time | Completed repeats |
|---|---|---:|---:|---:|
| Hot | Full basebackup | 1,610.16 MB | 131.49 s | 3/3 |
| Hot | Native incremental + combine | **38.65 MB** | **5.61 s** | 3/3 |
| Hot | Optimized pgdelta | **51.90 MB** | **13.80 s** | 3/3 |
| Hot | Full tar + extraction | 1,611.08 MB | 132.26 s | 3/3 |
| Uniform | Full basebackup | 1,607.84 MB | 129.25 s | 2/3 |
| Uniform | Native incremental + combine | 1,065.85 MB | 90.21 s | 2/3 |
| Uniform | Optimized pgdelta | 1,079.12 MB | 95.03 s | 1/3 |

These are medians of completed runs. Optimized pgdelta sent **96.8% less than a full backup for hot updates** and about **32.9% less for uniform updates** in these cells. The larger run has no matching pre-fix pgdelta arm, so use the paired pilot above to quantify the fix itself. Short laptop timings varied substantially in some cells. [Larger run and repetition counts](../runs/network-shutdown-scale100-20261120/report.md)

## Presentation FAQ

**Why did pgdelta originally send more than a full backup?**

It copied the stopped server directory, including accumulated or preallocated WAL segments. PostgreSQL’s backup process selects the files and WAL needed for its backup. Extra WAL could outweigh pgdelta’s database-page reuse.

**Why retain any WAL after a clean shutdown?**

PostgreSQL still needs a valid checkpoint/WAL state for startup. The selector preserves the segment containing the verified shutdown checkpoint.

**Does this support every kind of snapshot?**

Selection currently covers cleanly stopped PostgreSQL 17 primary snapshots on timeline 1, with 16 MiB segments and a checkpoint safely contained within one segment. Recovery states, base backups, replication slots, symbolic links, other timelines, and unsupported boundaries retain all WAL. Missing or invalid required checkpoint WAL fails the transfer.

**Are we reusing an existing backup?**

Yes. The destination already has an older backup B. Pgdelta compares source chunks with B at the same filename and offset, sends differences, and builds a new verified output O.

**Why use pgdelta if native incremental wins?**

Native incremental requires the matching backup manifest and retained WAL summaries. Pgdelta compares contents directly and can reuse copies without that history. Native incremental used fewer bytes in our measured cells. The missing-history scenario still needs its own benchmark with this implementation.

**How do we judge correctness?**

Native outputs pass `pg_verifybackup`. Pgdelta checks whole-file digests against the source. Every completed trial starts PostgreSQL from its output, compares ordered SHA-256 exports of all four pgbench tables with the expected source data, and runs `pg_amcheck`. This covers our fixture and supported shutdown workflow. Concurrent application writes and arbitrary schemas were outside these trials.

**How can Python count traffic from PostgreSQL’s C programs?**

Their connections go through a localhost TCP relay. Python counts the payload bytes crossing that relay in each direction, including the separate WAL-streaming connection. It also applies the bandwidth cap. TCP/IP headers and retransmissions are outside the count.

**Was Docker or GCP used?**

These runs used native Homebrew PostgreSQL 17 processes on the laptop and localhost TCP. They added bandwidth caps without artificial WAN latency. Cloud and WAN behavior need separate measurements.

**Which versions were tested?**

The paired pilot used remote baseline `fd3423c` plus the experimental source selector. The integrated larger run used local commit `6f675c473855c044d7d1516a54d1c45827dca609`. The feature on this branch is opt-in. This branch preserves the measured implementation and its comparison with the November rebuild.

**Can we reproduce the four-method matrix?**

From the repository, choose a fresh result directory and run the following. It performs 72 trials across two workloads and three link caps. The report and per-trial evidence are written to the result directory.

```bash
python3 -m benchmarks.laptop_network \
  --pgbin /opt/homebrew/opt/postgresql@17/bin \
  --wal-mode shutdown \
  --scale 100 \
  --result runs/presentation-wal-scale100
```

The previous large run’s generated databases have been removed. Small reports, logs, configuration, and statistics remain available.
