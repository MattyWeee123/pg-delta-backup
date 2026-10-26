# EXP-38: How old can the copy be before pgdelta stops beating a full copy at DR?

Status: **pre-registered October 26, 2026, before the counted run** (a smoke at scale 2 checked the script). Ledger test T11b, lean: the pitch by the age of the old copy. Local (the Mac): bytes measured, DR time projected from EXP-24's measurements.

## Question

pgdelta sends the pages that changed since the old copy was taken. The older the copy, the more pages have changed, until pgdelta sends about what a full copy does, plus the cost of hashing both. At DR round trips, how many transactions of a workload can an old copy be behind before pgdelta stops winning, for scattered and for local changes?

## Design

`experiments/bytes/exp38.py`:
- **The template:** `pgbench -i -s 20` (about 300 MB of relation files).
- **Per workload:** a clone is B0, the old copy, and S runs the workload in steps to 1,000, 3,000, 10,000, 30,000, 100,000 and 300,000 transactions (8 clients). After each step S is checkpointed and stopped, and the bytes pgdelta would send (its positional page comparison) are measured against B0.
- **Workloads:**
  - **tpcb:** pgbench's built-in TPC-B-like script, every account equally likely;
  - **hot:** the same script with the account drawn from the first 1%.
- **The projection,** fixed now from EXP-24 (60 ms, tuned hosts, 2.1 GB, BBR v1). Two medians, hot 3.63 s for 29.0 MB and uniform 6.62 s for 610.4 MB, give v1 ≈ 3.48 s + changed MB ÷ 194.4 MB/s. v1 then equals one `pg_basebackup` stream (11.63 s) at **75.3%** of the database changed, and the fastest parallel copy (10.48 s) at **64.7%**. The fraction is assumed to carry across database sizes, since both costs grow with size.
- **The analysis,** committed before the data: `analysis/exp38.py`.

## Predictions

1. **Deciding, tpcb:** the changed share reaches 75% within 100,000 transactions. At 1,000 transactions a second that's under two minutes, so for uniformly scattered OLTP, pgdelta beats a full copy at DR only if the old copy is refreshed every few minutes.
2. **hot:** the changed share stays under 10% through 300,000 transactions. For local workloads, pgdelta keeps its DR lead at any age measured.
3. tpcb's changed share keeps growing past 75% without reaching 100%. Indexes and the free-space map follow the table, but some pages (the branches, and parts of the history) stay put.

## What follows

- **If 1 and 2 hold:** the pitch is stated by workload. For local changes, pgdelta's DR lead holds whatever the copy's age. For uniformly scattered OLTP, it needs frequent refreshes, minutes behind at a thousand transactions a second, and beyond that a full copy (or native incremental, where history exists) is as good. The deployment note gives the crossover as a share of pages changed, which an operator can estimate.
- **If tpcb's share stays far below 75%:** pgdelta's DR lead lasts much longer for OLTP than feared, and the pitch widens.

## Limitations

- One synthetic workload per pattern; real OLTP mixes hot and cold data.
- Bytes are measured, and time is projected from one condition's model, not measured at each age.
- The projection is for BBR v1 on EXP-24's VMs; BLAKE3 would shorten c and move the crossover up.
