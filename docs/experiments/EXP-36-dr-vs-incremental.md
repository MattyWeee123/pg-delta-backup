# EXP-36: Where history exists, does native incremental beat v1 at DR round trips too?

Status: **pre-registered October 25, 2026, before any run.** Ledger test INC, lean: D02's question at the DR condition pgdelta is positioned for. One VM pair (the default set: two `n2-standard-8` with local NVMe, as EXP-20 and EXP-24), about an hour.

## Question

PostgreSQL 17's native incremental backup (`pg_basebackup --incremental` against a previous backup's manifest, then `pg_combinebackup` on the destination) needs history: WAL summaries on the source since a backup it knows. pgdelta's niche is the copy without that history. Where history does exist, EXP-04b found native incremental faster than pgdelta in all 42 workload-by-speed cells, at 0.1 ms. At DR round trips v1 now has BBR on its socket (EXP-24), BLAKE3 where available, and one-pass verification in place, while incremental runs on the host's cubic. Does incremental still win there?

## Design

EXP-24's harness command (`run_exp04.py`) with:
- **conditions:** `2:60:default`, `2:60:tuned` and `2:120:tuned` (2 Gbit/s; 60 and 120 ms; Debian's TCP buffers, or maxima of 128 MiB);
- **patterns:** hot and uniform 1% at about 2.1 GB (scale 70);
- **methods:**
  - `full`, one `pg_basebackup` stream, the yardstick;
  - `incremental`, native incremental, then `pg_combinebackup` to a new copy;
  - `pgdelta_direct_inplace_bbr`, v1 asking for BBR.
- **v1's settings:** v1 now runs with the command line's defaults, so `--verify-scope written` in place, and `--hash auto`. The VMs' system Python has no `blake3`, so that means BLAKE2b.
- **The one host setting, as EXP-24's:** the source allows BBR for unprivileged sockets; its default stays cubic.
- **Runs:** 2 repeats, order seed 360, CRC32C manifests, EXP-24's link-test and noise settings. Every output is verified by the harness.

## Predictions

1. **Deciding: native incremental takes at most v1's time in every cell** (3 conditions × 2 patterns). It reads only the changed blocks, found from the WAL summaries; v1 reads both copies whole.
2. For hot 1%, incremental sends at most a third of v1's bytes. Both send only changed pages, but v1 also sends its digests' worth of protocol.
3. For uniform 1% on default hosts at 60 ms, both are bound by one window-limited stream.

## What follows

- **If 1 holds:** D02 is firm at DR too. Where WAL summaries and a previous backup's manifest exist, native incremental is the tool, and pgdelta is for the copies they can't serve: lost history, recycled WAL, a copy from elsewhere. The deployment note says so plainly.
- **If v1 beats incremental in some cell:** the cell and its cause are reported. A likely cause is incremental's one cubic stream falling into the early-loss slow mode on scattered changes (EXP-21, EXP-23), where v1's BBR doesn't. pgdelta's DR role then also covers that case, provisional by rule 11 if the ratio lies within 20% of 1.

## Limitations

- One session, 2 repeats, 2.1 GB, local NVMe.
- Incremental's cost of keeping history (`summarize_wal` on, a manifest kept) isn't measured.
- Incremental runs as released: the host's cubic and default buffers on default hosts. The comparison is of the tools as they ship.
