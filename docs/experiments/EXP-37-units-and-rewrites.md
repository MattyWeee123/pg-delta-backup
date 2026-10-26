# EXP-37: What do pgdelta's whole, positional 8 KiB pages leave on the table?

Status: **pre-registered October 26, 2026, before the counted run** (a smoke at scale 2 informed the predictions, and says so). Ledger test T4, lean: its three open rows. Local (the Mac); no network, only bytes and CPU.

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
