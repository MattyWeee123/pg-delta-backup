# EXP-37: What do pgdelta's whole, positional 8 KiB pages leave on the table?

Status: **done October 26, 2026 (scale 10, 30 s; all four predictions held): after a rewrite, neither cross-file page matching nor content-defined chunking found a single reusable byte, so positional pages lose nothing there. For scattered updates, 512-byte blocks would send 5.0× fewer bytes and zstd 2.8× fewer, at 520–550 MiB/s per core.** Pre-registered before the counted run (a smoke at scale 2 informed the predictions, and says so). Ledger test T4, lean: its three open rows. Local (the Mac); no network, only bytes and CPU.

## Questions (the ledger's open rows)

- **D17:** pgdelta sends whole 8 KiB pages. How many fewer bytes would a sub-page delta send?
- **D18:** pgdelta compares page i of a file with page i of the same file in B. After a rewrite (`VACUUM FULL`, `CLUSTER`) a table gets a new file. Would content-addressed matching across files, or content-defined chunking, find the old bytes?
- **D35:** pgdelta doesn't compress. What would zstd save on the pages it sends, at what CPU?

## Design

`experiments/bytes/exp37.py`.
- **The template cluster:** `pgbench -i -s 10` (about 160 MB of relation files). pgbench's blank-padded `filler` is overwritten with hex text, since padding compresses and deduplicates unrealistically well. An index on `abalance` is added, then `VACUUM FULL ANALYZE` and a checkpoint.
- **Each scenario:** B0 and S are clones of the template; S is changed, checkpointed and stopped.
  - **uniform:** `UPDATE` 1% of `pgbench_accounts`' rows, chosen at random;
  - **vacuum_full:** the same, then `VACUUM FULL pgbench_accounts`;
  - **cluster:** the same, then `CLUSTER pgbench_accounts USING` the `abalance` index, which rewrites it in another order.
- **The bytes each strategy would send,** over S's relation files:
  - **positional:** pgdelta's;
  - **content pages:** pages found nowhere among B's pages;
  - **cdc:** content-defined chunks absent from B's, averaging 8 KiB;
  - **subpage:** the changed 64-byte blocks of the pages positional sends, a lower bound;
  - **subpage512:** the changed 512-byte blocks, plus B's block digests sent the other way;
  - **zstd:** level 3 on positional's pages, and its CPU.

## Smoke (scale 2, not counted)

| Scenario | positional | content pages | cdc | subpage (64 B) | subpage512 + digests | zstd of positional |
|---|---:|---:|---:|---:|---:|---:|
| uniform | 17.0 MB | 17.0 | 24.7 | 1.3 | 3.2 + 0.3 | 6.0 (2.85×) |
| vacuum_full | 33.0 | 33.0 | 33.4 | 32.8 | – | 12.1 |
| cluster | 33.0 | 33.0 | 33.4 | 32.8 | – | 12.1 |

## Predictions (at scale 10, informed by the smoke)

1. **D18, deciding:** after `VACUUM FULL` or `CLUSTER`, content-addressed pages and CDC each recover under 5% of positional's bytes. A rewrite changes every tuple's header (its `t_ctid`), so no page or chunk of 2 KiB or more survives byte for byte.
2. **D17:** for uniform updates, subpage512 plus its digests sends at most a third of positional's bytes.
3. **D35:** zstd shrinks positional's pages at least 2× on this data, at over 200 MiB/s per core.
4. For uniform updates, CDC sends more than positional: a changed byte resends its whole chunk.

## What follows

- **If 1 holds:** D18 closes. Positional matching loses nothing that cross-file or content-defined matching would find, on rewrites at least, and pgdelta stays positional. A rewritten table is sent whole, as by any tool.
- **If 2 holds:** a sub-page delta (512-byte blocks, digests from B for the pages that differ) is the largest bandwidth lever left for DR, worth a design: about 3× fewer bytes for scattered changes. It costs a round of block digests per page, which matters at DR round trips.
- **If 3 holds:** compression (`zstd` on the wire) is worth an option for links slower than its CPU rate.
- These are bytes on one synthetic dataset. Real tables, TOAST and indexes, would change the ratios, and Omni's data may be compressed already.

## Results (scale 10: 196 MB of relation files)

Data: `docs/experiments/data/exp37-bytes.csv`; raw `results/bytes/exp37/results.json`; figure `analysis/exp37.py`.

![The bytes each strategy would send, per scenario](../figures/exp37-bytes.svg)

| Scenario | positional (pgdelta) | content pages | cdc | subpage (64 B) | **subpage512 + digests** | **zstd of positional** |
|---|---:|---:|---:|---:|---:|---:|
| UPDATE 1% at random | 84.7 MB | 84.7 | 122.3 | 6.1 | **15.6 + 1.3 = 16.9** | **29.9** (2.83×, 548 MiB/s per core) |
| ... then VACUUM FULL | 163.9 | **163.9** | **164.3** | 163.8 | 163.8 + 0.003 | 60.3 (2.72×) |
| ... then CLUSTER | 164.0 | **164.0** | **164.3** | 163.8 | 163.8 + 0.004 | 60.3 (2.72×) |

*Corrected before the write-up:* the first run charged B's block digests for every page sent, new files' too, which no scheme would ask for. Rerun charging only pages with a page of B at their offset. The uniform result was unchanged, and the rewrites' digest bytes fell from 2.6 MB to a few kilobytes.

| # | Prediction | Verdict |
|---|---|---|
| 1 | **D18: after a rewrite, content pages and CDC each recover under 5%** | **Held:** both recovered nothing (0.0%) |
| 2 | **D17: for uniform updates, subpage512 + digests at most a third of positional** | **Held:** 16.9 MB against 84.7 (20%, 5.0× fewer) |
| 3 | **D35: zstd at least 2× at over 200 MiB/s per core** | **Held:** 2.83× (2.72× after rewrites) at 517–548 MiB/s |
| 4 | For uniform updates, CDC sends more than positional | **Held:** 122.3 against 84.7 MB |

## What follows

- **D18 closes:** positional matching stays. A rewritten table changes every tuple's header, so neither cross-file matching nor content-defined chunking finds old bytes. A rewrite is sent whole by any such tool.
- **D17 and D35 open two bandwidth levers for DR's slow or billed links.** Neither is built:
  - **a sub-page delta** (512-byte blocks: about 5× fewer bytes for scattered changes, at the cost of one more round of block digests from B per differing page, which matters at DR round trips);
  - **zstd on the wire** (about 2.8× on this data, at about 0.5 GB/s per core, well above the 2 Gbit/s DR link).
  Together they would act on different things, and their product isn't measured. They matter where bytes cost: links below about 1 Gbit/s, and billed egress.
- **The usual limits:** one synthetic dataset (pgbench with hex filler), one change pattern. Real tables, TOAST and data that is already compressed would change the ratios.
