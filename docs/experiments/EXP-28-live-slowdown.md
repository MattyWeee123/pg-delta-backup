# EXP-28: How much does v1 reading a live primary slow it, against one `pg_basebackup` stream?

Status: **pre-registered October 25, 2026, before any run.** Ledger test T10b, lean: the one decision it serves, with v1 itself on a live source (built since T10a, which could only measure v1 by proxy) and the load's client on its own VM. Three VMs (`PAIR=t10b`: source and dest `n2-standard-8` with local NVMe as EXP-12's, client `n2-standard-4`; 20 vCPUs), about 45 minutes with set-up.

## Question

On a busy primary, does v1's live refresh (`--source-kind live`, in place) slow the load more than one `pg_basebackup` stream, by O4's two numbers, and with how many source workers does it meet O4? That number is the default for live mode.

## What EXP-12 left

By proxy, v1's source side at 4 workers lost 0.05–0.07 s of the load's work and missed O4 at 0.6 of capacity, while 1 worker met it in twice the time. It was fragile, because the load's client shared the source's CPU, which charges CPU-heavy arms more than an application on another host would feel. Here the client runs on its own VM, and v1 runs for real.

## Design

- **The source:** EXP-12's cluster: EXP-01's dataset at scale 70 (about 1 GB) and `loaddb` (EXP-12's bank schema, scale 2), with EXP-12's server settings (`shared_buffers` 128 MB, no checkpoint inside a window but an arm's own). Set up by `source_cluster.py setup` and `loaddb-setup`, unchanged; one added `pg_hba` line lets the client VM in.
- **The load:** EXP-12's bank scripts (`commit.sql` at 99, `abort.sql` at 1), open loop: `pgbench -n -M prepared -c 32 -j 4 -R R`, from the client VM over the network, every transaction logged to the client's tmpfs. R = 0.6 × C, where C is the median of three 30-second closed-loop runs at 32 clients after a 30-second warm-up. The load runs through the whole grid.
- **The old copy:** a `pg_basebackup` of the source taken at set-up. Every v1 run refreshes it in place from the live source, so each run's B is the previous v1 run's output, minutes old: the frequent-refresh case. `pg_basebackup` copies into a fresh folder each time, deleted after the run's tail.
- **Arms:**
  - `none`: 8 s of load only;
  - `full`: one `pg_basebackup` stream, `-X stream -c fast`;
  - `v1_j1`, `v1_j2`, `v1_j4`: v1 live with `--jobs` 1, 2 and 4 on the source, and 4 on dest.

  Both `full` and v1 take a fast start checkpoint.
- **Speeds:** unthrottled, and 2 Gbit/s between source and dest (`wan.sh`, which leaves the client's traffic alone).
- **Runs:** 5 arms × 2 speeds × 2 repeats = 20, in an order seed 280 shuffles. Each run is a 15 s settle (load only, the receiver already listening), the timed arm from t0 to t1 on dest's clock, and a 10 s tail.
- **O4's two numbers,** as EXP-12's change 3 defines them, from the client's log over [t0, t1 + 10 s] in 1-second buckets, mapped to dest's clock by chrony's offsets (each VM's offset from the same reference, the metadata server):
  - the worst drop **D** = max(1 − X_b/S_b);
  - the work lost **W** = Σ max(0, S_b − X_b)/R,

  where S_b counts the transactions scheduled in a bucket and X_b those completed in it. Net values subtract the same speed's median over the `none` runs; below 0 counts as 0.
- **"Meets O4"** (EXP-12's floors): W_net(arm) − W_net(full) ≤ δ_W = max(0.05 s, 0.15 × W_net(full)), **and** D_net(arm) − D_net(full) ≤ δ_D = 0.03. Verdicts are also given at half and twice the floors (K7).
- **The lean simplification,** stated: the null is the `none` runs' median, not EXP-12's block bootstrap.
- **Extra repeats:** an arm whose excess over `full` lies within 20% of its δ on either number gets two more runs at that speed.
- **Also reported, not part of the verdict:** refresh time, the source's VM-wide CPU-seconds (`/proc/stat`), and the p95 and p99 latency over the window.
- **Committed before the data:** `experiments/network/lab_exp28.sh` (the driver, on dest) and `analysis/exp28.py`.

## Predictions

1. `full` costs the load little: W_net under 0.1 s at both speeds. EXP-12 measured nothing beyond its null, with the client on the source.
2. `v1_j1` and `v1_j2` meet O4 at both speeds.
3. **Deciding: `v1_j4` meets O4 at both speeds.** With the client off the source, v1's CPU no longer delays the load's client. EXP-12's proxy miss (0.05–0.07 s over `full`) was about the size of the floor, and was flagged as client wait.
4. v1's refresh time falls with workers (j4 < j2 < j1), and at 2 Gbit/s v1 at 4 workers is faster than `full`: the copy changes only `loaddb`'s pages.

## What follows

- **If 3 holds:** live mode keeps pgdelta's default source workers (min(4, CPUs)). O4's cap for v1 is stated as met at 4 workers on this load, with the client off the source.
- **If `v1_j4` misses but a smaller count meets:** live mode's default drops to the largest count that meets O4 at both speeds, and the time it costs is reported.
- **If even `v1_j1` misses:** v1 on a busy primary is slower on the load than `pg_basebackup`. That is reported as a cost of the design, beside its speed and bytes.
- **One session.** A deciding excess within 20% of its δ is provisional, and that cell gets a second session (K10, as narrowed).

## Limitations, known before running

- **One load** (EXP-12's bank load at 0.6 of capacity), one cluster size (about 1 GB, mostly static), one machine shape. EXP-12's moderate level and its large-dirty-set cluster are not run.
- **The null is the `none` runs' median,** not a bootstrap over load-only time, so "measurable" rests on EXP-12's floors.
- **Each v1 run refreshes a copy minutes old.** A refresh after hours of changes reads and sends more.
- **One session.**
