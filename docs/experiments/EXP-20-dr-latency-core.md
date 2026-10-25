# EXP-20: Does v1 still beat the full copies at disaster-recovery round trips? (EXP-13's deciding cells, one session)

Status: **pre-registered October 25, 2026, before running.** One fresh VM pair, about 1.5 hours. It runs only the cells that decide [EXP-13](EXP-13-latency-transport.md)'s DR verdict, under the lean plan (rule 11 as clarified October 25). EXP-13's side arms (TLS, window and high-churn arms, `pg_probackup`, 1 Gbit/s, uniform 10%) and its rule 11 revision stay on hold; they are what remains of EXP-13 after this.

## Why this subset

v1's first target is DR's slower, longer links ([What T2 settled](../DECISIONS.md#what-t2-settled-october-24-2026)). Every measurement so far was at 0.1 ms. EXP-13's DR verdict decides whether v1's lead holds at 20, 60 and 120 ms, and it needs only v1, the fastest parallel copy (rule 3's yardstick) and one `pg_basebackup` stream (the released tool's), at 2 Gbit/s, on default and tuned hosts.

## Design

- **VMs:** one fresh `n2-standard-8` pair in `us-central1-a` (EXP-13's zone), local NVMe RAID 0, ext4, Debian 12; EXP-13's harness (the emulated WAN, `wan.sh`; the control path on port 2222; the sysctl guard).
- **Conditions** (`RATE:RTT_MS:HOST`): 2:0, 2:20, 2:60, 2:120 on default hosts; 2:60 and 2:120 tuned (128 MiB TCP buffers); the tuned anchor 2:0:tuned.
- **Arms:** `pgdelta_direct_inplace` (v1, 4 jobs, `--window 4`), `full` (one `pg_basebackup` stream), `parcopy8` (8-stream parallel copy, the bound) at every condition but the tuned anchor, and `parcopy16` at 2:120:default, where 8 streams may be window-limited (as EXP-13 registered).
- **Workloads:** hot and uniform 1% (scale 70, about 2.1 GB). **3 repeats.** Order seed 200.
- **Durability and verification:** EXP-13's, unchanged; check (a)'s limit 4 MiB at every window length (EXP-13's value until its smoke item 18).
- **A short smoke first** (scale 5, hot, 1 repeat, at 2:0:default, 2:60:default and 2:60:tuned), then the run.

## Predictions and rules: EXP-13's, as registered

- **The verdict** is EXP-13's DR verdict, rows in order, from `R_fast` (v1 ÷ the fastest durable parallel copy) and `R_F0` (v1 ÷ one `pg_basebackup` stream) per workload and host setting at each r. EXP-13's expected outcome (E13): at 20 ms, row 1 ("holds as shipped"); at 60 ms, row 2 ("holds with tuned buffers"): hot 0.33–0.57 on default hosts, uniform 1% 1.43–1.94 on default and 0.57–0.80 tuned; at 120 ms, row 2 or Undecided; the qualifier (v1 faster than one `pg_basebackup` stream) at every r.
- **What this subset can't judge:** EXP-13's rules that need its side arms (TLS, windows, the high-churn fallback, `pg_probackup`, 1 Gbit/s, uniform 10%) are "not run here".
- **Under rule 11 as clarified,** one session decides a row only where its deciding ratio sits clear of the drift measured between sessions (EXP-16: up to about 20% in CPU-bound cells; within 2% where the link sets the pace). A deciding ratio inside that margin of 0.90 or 1.00 is reported as provisional.

## Limitations

An emulated WAN in one zone (EXP-15 would check a real link). One session, one VM type. 2.1 GB in page cache.
