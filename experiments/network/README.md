# Experiment 4: refresh methods over throttled links

## Question

On two real machines, at what link speed does pgdelta stop beating a full copy? EXP-02 counts bytes and CPU on one machine. This experiment adds the network: two VMs, a link shaped to several speeds, and end-to-end time for each method, with bytes on the wire counted at the network interface rather than taken from each tool.

## Setup

- **Machines:** `pgd-source` and `pgd-dest` (see [infra/gcp/](../../infra/gcp/README.md)).
  - `n2-standard-8` with Debian 12 and PostgreSQL 17 from PGDG, rsync 3.2.7, Python 3.11 and GNU time 1.9 (Debian package `time`, for each tool's peak memory).
  - Local NVMe SSDs striped at `/data`: 4 per VM, about 1.6 GB/s writes and 2.0 GB/s reads with direct I/O. The design asked for 2, but the zone had no stock for that.
  - Both VMs are in one zone and talk over internal addresses. MTU 1460, virtio_net, 8 queues. A single TCP stream between them runs at 13 to 15 Gbit/s unthrottled.
- **Control:** `run_exp04.py` runs on `pgd-dest`, the receiving side. It drives `pgd-source` over one multiplexed SSH connection with a key made on `pgd-dest` for this purpose.
  - `agent.py` runs on both VMs for counters, `tc`, cache drops, link tests and tree digests.
  - `source_cluster.py` runs EXP-01's cluster and workload code on the source.
  - `remote.py`, run from the Mac, deploys the code, starts the orchestrator under `nohup` and fetches the results.
- **Code on the VMs:** a tarball of the working tree's tracked files, unpacked at `~/pgd`, with `DEPLOYED.json` (commit, dirty flag, tree hash). A run refuses to start if the two VMs hold different trees.

## Procedure

Each **trial** is a (pattern, fraction, scale) triple. The defaults are scale 70 and EXP-01's seed, as in EXP-01/02.

1. **Source cluster.** A fresh cluster on `/data/exp04/src`, built by EXP-01's `run_study.Trial`:
   - initdb with data checksums, locale C;
   - `wal_level = replica`, `max_wal_senders = 10`, `summarize_wal = on`, defaults otherwise.

   Two settings are added so the destination can reach it: `listen_addresses` = the source's internal address, and `pg_hba.conf` trust for normal and replication connections from the destination's address only. Then `pgbench -i -s SCALE` plus the `events` table, `VACUUM ANALYZE`, row counts and `CHECKPOINT`, exactly as in EXP-01.
2. **B0**, the stale old copy, is taken on the destination over the unthrottled link:
   - `pg_basebackup -Fp -X stream --manifest-checksums=SHA256 -c fast`;
   - checked with `pg_verifybackup`;
   - hashed in full (SHA-256 per file).

   Nothing writes to it afterwards. A cheap stat fingerprint of B0 is compared after every method run, and the full hash at the end of the trial. A change fails the run or the trial.
3. **Workload:** EXP-01's generator (`workloads.py`): the same SQL, seed and 100-statement transactions. Then `CHECKPOINT`. Row-level ground truth (EXP-01's `verify_rows`) is checked only after the last method. A count before then would set hint bits and change pages after the checkpoint.
4. **For each repeat**, the link speeds are visited in a new order drawn from a seeded `random.Random` (`--speed-order shuffle`, the default; `alternate` runs the list forwards then backwards, `ascending` as given). So drift during a long sweep is not confounded with link speed. Each repeat's order is recorded in `run.json` (`speed_orders`), in `trials/<trial>.json`, and in every run record (`speed_order`, `speed_index`). **At each speed**:
   - `infra/gcp/throttle.sh` puts a `tc` token-bucket filter on the egress of **both** VMs' internal interface, so both directions are shaped. The default sweep is 0.1, 0.25, 0.5, 1, 2, 5 and 10 Gbit/s, plus `unthrottled`, which is the default `mq` qdisc.
   - The achieved rate is then measured in each direction with a 3-second single-stream bulk TCP transfer (`agent.py blast-send/blast-recv`) and recorded.
   - Then every method runs once, in an order drawn from `random.Random(--order-seed)`, a new order for every (repeat, speed).
5. **Each method run:**
   1. Remove leftovers.
   2. Untimed set-up: pgdelta's stats directory on the source; the rsync daemon on dest.
   3. `sync; echo 3 > /proc/sys/vm/drop_caches` on both VMs, once, recorded with timestamps in `cache_drop`.
   4. The timed phases (below).
   5. Untimed verification.
   6. Delete the outputs.

| Method | Timed phases | Verification (untimed) |
|---|---|---|
| `full` | **transfer**: on dest, `pg_basebackup -h source` into a new directory | `pg_verifybackup` |
| `incremental` | **capture**: on dest, `pg_basebackup --incremental=B0/backup_manifest`; **combine**: `pg_combinebackup --copy --manifest-checksums=SHA256 B0 I -o O` | `pg_verifybackup` on O |
| `pgdelta` | **receiver_capture**, one timed phase with two parts that run at the same time: the **receiver** on dest, `pgdelta receive --basis B0 --listen dest:7000 --prefetch-basis on`, from its launch until its log says it is listening (it then reads B0 into the page cache while it waits for the sender); and the **capture** on source, local `pg_basebackup` S into `/data/exp04/S` over the Unix socket. **transfer**: `pgdelta send --source S --connect dest:7000` on source, until both ends exit. Both ends run with `--pg-controldata` (reuse mode) and EXP-06's engine pinned by its flags: `--jobs 4` on both ends (4 worker processes each), and `--rebuild copy-patch --verify pipelined` on the receiver. That is also the command line's default on these 8-vCPU VMs, but the flags keep a later change of the defaults from changing what `pgdelta` measures | `pg_verifybackup` on O; O byte-identical to S (SHA-256 of every entry on both VMs); O shares no inode with B0 and has no hard links |
| `pgdelta_1thread` (optional) | as `pgdelta`, receiver beside the capture and prefetch included, with EXP-02's single-threaded engine pinned (EXP-06's baseline, and what EXP-04's smoke runs measured as `pgdelta`): `--jobs 1` on both ends, `--rebuild gap-fill --verify sequential` on the receiver. Not in the default `--methods` | same as pgdelta |
| `rsync_8k`, `rsync_default` | **prep_capture**, one timed phase with two parts that run at the same time: the **prep** on dest, `cp -a B0 O`, then `sync -f O` (syncfs of O's filesystem), so the copy is on disk inside the timer; and the **capture** on source, as for pgdelta. **transfer**: on source, `rsync --archive --checksum --no-whole-file --inplace --delete --stats [--block-size=8192] --fsync S/ rsync://dest:8730/exp04/<method>/`, to an rsync daemon on dest (unencrypted, like pg_basebackup and pgdelta), started before the cache drop and not timed | same as pgdelta |

The staged methods (pgdelta and both rsync variants) each take their own fresh capture S, so each run's capture time is its own. S is written just before its transfer reads it, so the transfer reads S partly from the page cache. That is what a staged v0 refresh would do. B0 starts cold for every method. rsync's O is written by `cp` just before the transfer and stays in the page cache, so rsync's receiver reads it warm, as it would in a real cp-then-rsync refresh. pgdelta's receiver reads B0 into the page cache while it waits for the sender, so it hashes B0 warm, as far as the wait allowed. There is no second cache drop.

**rsync's copy of B0 runs beside its capture.** The prep uses only pgd-dest and the capture only pgd-source, and the link is idle during both, so a real cp-then-rsync refresh would run them at the same time. EXP-04's methods table puts the rsync after both steps but doesn't order the two steps against each other. So the harness starts both together after the one cache drop, and rsync's end-to-end time is **max(prep, capture) + transfer**. The copy is on rsync's critical path only where it takes longer than the capture. pgdelta's counterpart, writing a whole new O, happens inside its transfer, alongside the network and the hashing. This choice is recorded in [EXP-04](../../docs/experiments/EXP-04-network-sweep.md#methods-and-what-is-timed).
- The first fairness-fix smoke run (below) ran the two one after the other, with a cache drop between them, and so added min(prep, capture) and a cold re-read of O to rsync alone. That serial sum is still recorded, as `serial_total_s`, the upper bound.
- At scale 5 the prep (about 0.5 s) is shorter than the capture (about 1.1 s), so it adds nothing to `total_s`. At scale 70 the copy of 2.1 GB should take about 5 to 6 s (cp at about 490 MB/s cold, `sync -f` at about 1.6 GB/s), so it may outlast the capture and be on the critical path.
- The two parts share only the multiplexed SSH connection that launches the capture. Each part's own wall and start offset are in the phase's `parts`; each command record names its `part`.

**pgdelta's receiver starts beside its capture, and reads B0 while it waits.** This is pgdelta's counterpart of rsync's overlap, decided after rsync's (see [EXP-04](../../docs/experiments/EXP-04-network-sweep.md#methods-and-what-is-timed)). Before, the receiver started only after the capture, inside the transfer timer, so its Python start-up, `pg_controldata` on B0 and scan of B0 were on pgdelta's critical path, and it then hashed B0 cold. A real refresh starts the receiver first and lets it wait, like rsync's daemon or the copy of B0.
- **One timed phase, `receiver_capture`, after the one cache drop.** The **receiver** part (dest) launches `pgdelta receive --listen` and ends when its log says `listening on`. The **capture** part (source) is the same local `pg_basebackup` as before. pgdelta's end-to-end time is **max(receiver start, capture) + transfer**, and `serial_total_s` adds the two parts instead, the upper bound, as for rsync.
- **The receiver keeps running after its part ends.** While it waits for the sender, it reads B0 into the page cache (`--prefetch-basis on`; see [pgdelta's README](../../pgdelta/README.md#prefetching-b-while-the-listener-waits)): read-only, in B0's inventory order, at most half of `MemAvailable` (about 15 GB here, so all of B0), and it stops when the sender connects. The transfer phase then starts the sender and ends when both ends have exited.
- **Both pgdelta methods prefetch.** Prefetching is a receiver feature that doesn't depend on how either end hashes, and with it off `pgdelta_1thread` would read B0 cold where `pgdelta` doesn't. Each record's `tool_reported.flags` holds `--prefetch-basis on`, `tool_reported.prefetch` the receiver's own statistics (bytes, seconds, how long it waited, why it stopped), and `prefetch_ok` whether the switch was the pinned one.
- **Its records.** The phase's `parts.receiver` holds the receiver's argv and log. Its command record, with its rusage and GNU time's report, sits in the transfer phase, since it exits there; its `wall_seconds` and CPU cover its whole life, from the launch in `receiver_capture`. If the capture fails, the waiting receiver is stopped outside any timer and its record is added to `receiver_capture`.
- In the smoke run below (scale 5), the receiver listened after 0.15 to 0.18 s, well inside the capture's 1.0 to 1.05 s, and read all of B0's 189 MB in 0.26 to 0.27 s. At scale 70 it should still listen within the capture; whether all 2.1 GB of B0 are read before the sender connects depends on the capture's length, and `prefetch_stopped` records it.

**Every method writes a complete, durable O inside its timers.** pg_basebackup and pg_combinebackup fsync their output by default, and pgdelta fsyncs every file and directory before it publishes O. rsync patches a copy of B0 instead, since ext4 has no instant clone, so its counterpart is that copy plus its writeback. Both are timed in the prep, and `sync -f` finishes the writeback inside the timer, so none of it is left to a later untimed `sync`. Then `--fsync` makes rsync fsync every file it writes. It doesn't fsync directories, but on ext4 an fsync of a new file also commits the journal transaction that holds its directory entry.

**Totals.** Verification is never timed.
- **`total_s`** is end to end: the sum of a method's timed phases, rsync's prep included, with `prep_capture` counted at the pair's wall, about max(prep, capture), and pgdelta's `receiver_capture` likewise, about max(receiver start, capture). This is the headline number in EXP-04's design, and `report.py` computes its ratios and crossover on it.
- **`serial_total_s`** (rsync and pgdelta) is the concurrent phase's two parts plus the transfer, one after the other. It is the upper bound. `report.py` shows it as `rsync_* (serial)` and `pgdelta* (serial)` rows.
  - For rsync it is prep + capture + transfer, as if the copy of B0 had to finish before the capture started.
  - For pgdelta it is receiver start + capture + transfer: the receiver is started first and listens before the capture starts, then the capture runs while the receiver waits and prefetches B0, then the transfer.
  - So the two serial bounds differ. rsync's has a cold read of B0 on its critical path (the copy), and pgdelta's has none. EXP-04's note says how N5 is read under the serial reading.
- **`inplace_total_s`** (rsync only) is capture + transfer, rsync without its prep: the lower bound for patching B0 directly, which is faster but leaves B0 half-updated if anything fails. O is warm here and B0 would be cold, which only loosens the bound. `report.py` shows it as `rsync_* (in place)` rows.
- **`total_no_ssh_s`**, **`serial_total_no_ssh_s`** and **`inplace_total_no_ssh_s`** are the same totals with the remote-launch overhead taken out (see **Wall time** below).
- `prep_s`, `capture_s`, `prep_capture_s` (the pair, rsync only), `receiver_start_s` and `receiver_capture_s` (the pair, pgdelta only), `transfer_s` and `combine_s` are the phases and parts.

## Measurements

For every timed phase, both VMs' counters are read just before and just after it:

- **Wall time:** `time.perf_counter()` on pgd-dest around the phase.
  - Remote commands go through SSH plus the agent's start-up. That overhead is about 60 ms per remote command, so about 0.12 s per staged run (capture and transfer), and local-only methods (`full`, `incremental`) have none.
  - It is included in `total_s` and reported as `ssh_overhead_s`: the sum over remote commands of the outer wall (on pgd-dest) minus the inner wall (the agent's, around the command itself, `inner_wall_seconds` in each command record).
  - In rsync's `prep_capture`, only the part of the capture's overhead that is on the critical path counts: max(part walls) minus max(part walls without their overhead). It is all of it when the capture is the longer part, and none when the prep outlasts the capture by more than the overhead.
  - `total_no_ssh_s` is `total_s` minus `ssh_overhead_s`, and equals `total_s` for local-only methods. `report.py` prints it as a second, labelled view; the headline crossover stays on `total_s`.
- **Bytes on the wire:** `/proc/net/dev` of the internal interface on both VMs.
  - `wire_s2d_bytes` is the source's transmit counter and `wire_d2s_bytes` the destination's.
  - The receive counters on the other side are kept as cross-checks (`wire_s2d_bytes_dest_rx`, `wire_d2s_bytes_source_rx`). In the smoke run:
    - source to destination agreed within 0.11%;
    - the small return direction (0.1 to 0.4 MB) agreed within 7%, because the SSH traffic of the counter reads lands at slightly different moments on the two VMs;
    - source to destination was 0.1 to 0.5% above the tools' own counts.
  - The idle background is measured once per run (`run.json` `background`): 0.1 to 3 KB/s, the higher figure while `remote.py wait` was polling from the Mac.
- **CPU:** VM-wide busy seconds from `/proc/stat` (user + nice + system + irq + softirq + steal) on each VM (`cpu_source_s`, `cpu_dest_s`), summed over the timed phases, rsync's prep included.
  - This includes PostgreSQL's walsenders, kernel network processing and the agent, which no per-process figure sees. **These are the columns to compare across methods.**
  - rsync's prep shares the `prep_capture` counter window with the capture. The prep runs nothing on the source, so it adds no source CPU; `prep_capture_cpu_dest_s` is dest's CPU in that window, which is the prep's plus the capture's SSH client. The in-place dest CPU can't be separated from the prep's, so `totals_in_place` leaves it unknown; `cpu_dest_s - prep_capture_cpu_dest_s` is a lower bound for it.
  - pgdelta's receiver shares the `receiver_capture` window with the capture in the same way. `receiver_capture_cpu_dest_s` is dest's CPU in that window: the receiver's start-up and its prefetch of B0 so far, plus the capture's SSH client. The prefetch can go on into the gap between the two phases, while the counters are read; that part's CPU is in neither window, but it is in the receiver's own rusage (`proc_cpu_dest_s`).
  - **The same gap affects wall time.** B0 that the prefetch reads in the gap is read off the clock: the transfer would otherwise have read it, so pgdelta's `total_s` can understate by about that time. No other method has destination work in the gap. rsync's copy of B0 ends inside `prep_capture`, and `full` and `incremental` run nothing there.
    - Each pgdelta record measures it as `prefetch_untimed` (CSV: `untimed_gap_s`, `prefetch_untimed_s` and `prefetch_untimed_bytes_est`). That is the overlap of the prefetch's reading with the gap, from the receiver's wall-clock start and end against the phases' `ended_utc` and `started_utc`, all on pgd-dest's clock. The bytes are estimated at the prefetch's average rate.
    - `report.py` lists every run where it is above 0. It is at most the gap, about 0.12 s, or about 85 MB at the smoke run's cold read rate of B0 (725 MB/s).
  - **Per-phase floor.** Every timed phase has its own counter window, and each window holds the exit of its "before" snapshot agent and the start of its "after" one (python3 over SSH on the source). `run.json` `phase_floor` measures this with three empty phases at start-up, per VM. The totals don't subtract it. `counter_windows` says how many windows a total holds: 1 for `full`, 2 for `incremental`, `pgdelta` and both rsync variants. (Before the overlap, rsync had a third, source-idle window for its prep.)
- **Per process:** `proc_cpu_*_s` is CPU from `wait4` of every command we launch, and `peak_rss_*_kb` the largest peak RSS among them on that VM.
  - Peak RSS comes from **GNU time** (`/usr/bin/time -f %M`), which every tool runs under. A child's own `ru_maxrss` can't be used: Linux carries the parent's RSS high-water mark into a forked child, so pg_basebackup, pg_combinebackup and rsync would report the Python launcher's memory.
  - Measured on pgd-dest from a parent holding 420 MB, `/bin/true` reported 420,644 KiB through `wait4` and 1,144 KiB under GNU time. Every run repeats this check at start-up on both VMs (`run.json` `rss_probe`) and refuses to start if GNU time is missing or reports more than 16 MiB for `/bin/true`.
  - The raw `wait4` value is kept in each command's `rusage` as `wait4_maxrss_kb`, for the record only.
  - **%M is the peak of the largest single process in a tool's tree, not the tree's combined footprint.** For children, `getrusage(2)` reports the largest child's `ru_maxrss`; it does not add them up. On pgd-dest, GNU time reported 68,952 KiB for two concurrent Python children of 60 MB each, against 68,844 KiB for one. So:
    - `pgdelta_1thread` (one process per end), pg_combinebackup, cp, sync and the sending rsync client are reported whole;
    - `pgdelta` at `--jobs 4` is not one process: each end starts 4 hashing worker processes beside its main process, and only the largest of the five is reported. Their CPU is in `proc_cpu_*_s`: pgdelta starts them with `spawn`, as its own children, and reaps them when its pool closes, so `wait4` of pgdelta includes them, and so do GNU time's user and system times. (Before the EXP-06 review, Linux got `forkserver`, whose workers are children of the fork server, which no one waits for, and `wait4` missed 85–90% of pgdelta's CPU at `--jobs 4`.) Each run's `tool_reported.worker_start_method` records the method both ends used. `pgdelta_1thread` (`--jobs 1`) starts no workers;
    - `pg_basebackup -X stream` forks a WAL-streaming child that runs beside its main process, and only the larger of the two is reported;
    - the rsync daemon forks a per-connection process (the generator), which forks the receiver (seen with `ps --forest` on pgd-dest during a push); the two run at the same time, and only the largest of the daemon, generator and receiver is reported.

    The multi-process tools' real concurrent footprint can be larger, up to the sum of their processes. A true tree peak would need a cgroup per tool (`memory.peak`) or sampling the tree's RSS; the harness does neither.
  - The rsync daemon's CPU includes its reaped generator and receiver (`wait4` rusage sums them). Signals to a tool under GNU time go to the tool's process group except GNU time itself, so GNU time always writes its report.
  - **Not comparable across methods.** Server-side work is not a child process: the walsender that serves `full` and `incremental` over the link, and the one that serves the staged methods' local capture, run inside PostgreSQL on the source, and no per-process column covers them. The methods also launch different clients, with different process structures (above). The `proc_scope` column says what each method's per-process figures cover.
- **Tool-reported bytes:**
  - pgdelta's `--stats-json` totals (frame bytes);
  - rsync's `--stats` from the sending client ("Total bytes sent/received");
  - none for pg_basebackup. For `full`, the byte reference is `output_minus_wal_padding_bytes`, not `output_bytes`, since O's WAL segment is zero-padded on disk but not on the wire (next item).
- **Sizes:** `output_bytes` (O) and `capture_bytes` (S, or the increment).
- **WAL:** per run, from the backup's `pg_wal` and its `backup_manifest`:
  - `wal_bytes`: the bytes of the WAL files. For the staged methods these are S's, from its digest on the source; otherwise O's.
  - `wal_stream_bytes`: the WAL a streaming `pg_basebackup -X stream` sends, from the start of the first segment to the manifest's `End-LSN`.
  - `wal_padding_bytes`: the difference, the zeros after the end of the WAL in the segment file.
  - `output_minus_wal_padding_bytes`: `output_bytes` minus that padding.

  Each staged capture S holds a new 16 MiB WAL segment that B0 lacks, written whole and zero-padded. pgdelta and rsync send it in full, while `full` and `incremental` stream only the real WAL bytes (a few KB). So **the staged design costs about 16 MiB (16.8 MB) more on the wire per run.**
  - At scale 5 that's over a quarter of the uniform 1% delta (about 59 MB).
  - At scale 70 it's about 3% of the uniform 1% delta (608 MB in EXP-02), but more than half of the hot and append 1% deltas (27–28 MB). So it weighs on prediction N3 (10× faster than a full copy at 0.1 Gbit/s) for exactly those workloads.
  - It's a cost of staging S as a plain backup, not of the delta algorithms. A staged method that read the WAL range instead of the padded file would avoid it.

## Running

From the Mac, in the repository root:

```sh
infra/gcp/create.sh                        # both VMs; about 1 USD/hour together
python3 experiments/network/remote.py wait-ready
python3 experiments/network/remote.py deploy        # after every code change
python3 experiments/network/remote.py setup-ssh     # once per pair of VMs

# smoke sweep (about 4 minutes)
python3 experiments/network/remote.py start smoke -- --scale 5 --patterns uniform,append \
    --fractions 0.01 --speeds 0.5,2,unthrottled --repeats 1
python3 experiments/network/remote.py wait smoke --poll 30
python3 experiments/network/remote.py fetch smoke --to results/exp04-smoke
python3 experiments/network/report.py results/exp04-smoke/summary.csv

# full sweep: defaults are scale 70, uniform,hot,append at 0.01, all eight speeds, all methods
python3 experiments/network/remote.py start full -- --repeats 3
python3 experiments/network/remote.py status full
python3 experiments/network/remote.py fetch full            # -> results/exp04/full

infra/gcp/teardown.sh                      # results on /data are lost: fetch first
```

`run_exp04.py --help` lists every option:
- the trial options `--patterns`, `--fractions`, `--scale`, `--seed`, `--hot-percent`, `--events-rows` and `--batch-size`;
- `--speeds` (Gbit/s values and `unthrottled`), `--methods` and `--repeats`;
- `--order-seed` (method orders, and speed orders from their own seeded stream), `--speed-order` (`shuffle`, `alternate` or `ascending`), `--link-test-seconds`, `--idle-seconds` and `--disk-test-mib`;
- `--gnu-time` (default `/usr/bin/time`; `none` skips peak memory);
- `--keep-outputs`, `--keep-cluster` and `--stop-on-error`.

`--methods` defaults to `full,incremental,pgdelta,rsync_8k,rsync_default`. `pgdelta` runs EXP-06's engine and `pgdelta_1thread` EXP-02's, each pinned by its flags (`measure.PGDELTA_ENGINES`), never pgdelta's defaults. Add `pgdelta_1thread` (for example `--methods full,incremental,pgdelta,pgdelta_1thread,rsync_8k,rsync_default`) to run both, as EXP-04's design asks once parallel hashing is merged. The run checks `pgdelta send --help` (`--jobs`) and `receive --help` (`--jobs`, `--rebuild`, `--verify`) on both VMs first and refuses to start without them. Each pgdelta record's `tool_reported` holds the flags passed, the `switches` pgdelta reported as in effect, and `switches_ok`, whether they are the pinned engine's.

`tpcb` is not supported here because pgbench is not a generated script. A second run refuses to start while one holds `/data/exp04/.exp04.lock`.

Cost and duration of the full sweep are estimated, not yet measured. At scale 70 (about 2 GB per backup), one trial at all eight speeds is dominated by the 0.1 Gbit/s cell and the per-run verification. That comes to roughly half an hour per trial and repeat, so the default three patterns with `--repeats 3` take about 4 to 5 hours, about 5 USD.

## Outputs

Everything goes under `/data/exp04-results/NAME/` on pgd-dest, and `fetch` copies it to the Mac:

```text
run.json                        args, both VMs' environment (machine type, CPU platform, kernel,
                                PostgreSQL and rsync versions, NIC, qdisc, TCP settings, /data,
                                deployed commit and tree hash), disk test, idle background,
                                per-phase counter floor (phase_floor), measured link rates per
                                speed, trial statuses, timestamps, speed orders per trial and
                                repeat, peak-RSS probe (rss_probe)
summary.csv                     one row per (trial, repeat, speed, method)
runs/<trial>/r<repeat>-<speed>-<method>.json
                                the full record: every phase's commands (argv, rusage, GNU
                                time's peak RSS), rsync's prep_capture and pgdelta's
                                receiver_capture parts, counters, the cache drop, totals (end
                                to end), totals_serial (rsync, pgdelta), totals_in_place
                                (rsync), WAL, tool statistics (pgdelta's prefetch included),
                                verification
trials/<trial>.json             cluster setup, B0 (time, bytes, digest), workload, speed orders,
                                per-speed tc settings, link tests and qdisc counters, ground
                                truth, B0 check
logs/<trial>/dest/, logs/<trial>/source/
                                every command's output, prefixed r<repeat>-<speed>-, with
                                GNU time's report beside it as <log>.time. Logs are named per
                                method: <method>-capture.log for each staged capture on the
                                source, <method>-mkdir-stats.log, and one rsync daemon log
                                per method (<method>-rsyncd.log, <method>-rsyncd-transfers.log)
logs/rss-probe/                 the start-up peak-RSS probe on pgd-dest
driver.log                      the orchestrator's progress log
```

`report.py summary.csv` prints, per trial:
- the median `total_s` of each method at each speed, the ratio to the full copy, and the first speed at which each delta method is no longer faster. This is the headline, and its crossover is computed on `total_s`.
- `rsync_* (serial)` and `pgdelta* (serial)` rows: the same for `serial_total_s`, the upper bound with the concurrent phase's two parts (rsync's copy of B0, or pgdelta's receiver start-up, and the capture) one after the other.
- `rsync_* (in place)` rows: the same for `inplace_total_s`, the lower bound without the copy of B0.
- a second, labelled view of the same tables on `total_no_ssh_s`, with the remote-launch overhead taken out.

CSV columns:
- `run_name`, `trial_id`, `pattern`, `fraction`, `scale`, `rows_changed`, `repeat`;
- `speed`, `speed_index` (its position in the repeat's speed order), `target_gbit`, and `measured_s2d_gbit` / `measured_d2s_gbit` from the link test;
- `method`, `order_index`, `status`;
- `total_s` (end to end), `total_no_ssh_s`; rsync only: `inplace_total_s`, `inplace_total_no_ssh_s`; rsync and pgdelta: `serial_total_s`, `serial_total_no_ssh_s`;
- `prep_s` (rsync only), `capture_s`, `prep_capture_s` (rsync only), `receiver_start_s` and `receiver_capture_s` (pgdelta only), `transfer_s`, `combine_s`, `ssh_overhead_s`;
- pgdelta only, from its receiver's statistics: `prefetch_bytes` (B0 read into the page cache while it waited), `prefetch_basis_bytes` (B0's size), `prefetch_s` (time spent reading), `prefetch_wait_s` (from the receiver listening until the sender connected) and `prefetch_stopped` (`finished`, `cap`, `error`, or `connected` if the sender came first);
- pgdelta only: `untimed_gap_s` (between `receiver_capture` and the transfer, untimed), `prefetch_untimed_s` (how long the prefetch read B0 in that gap, off the clock) and `prefetch_untimed_bytes_est` (the bytes, at the prefetch's average rate); `hash_basis_s` (the receiver hashing B0 for the sender, inside the transfer);
- `wire_s2d_bytes`, `wire_d2s_bytes` and the receive-side cross-checks;
- `tool_s2d_bytes`, `tool_d2s_bytes`, `output_bytes`, `output_minus_wal_padding_bytes`, `capture_bytes`;
- `wal_bytes`, `wal_stream_bytes`, `wal_padding_bytes`;
- `cpu_source_s`, `cpu_dest_s` (end to end), `prep_capture_cpu_dest_s` (rsync only), `receiver_capture_cpu_dest_s` (pgdelta only), `counter_windows`;
- `proc_cpu_source_s`, `proc_cpu_dest_s`, `peak_rss_source_kb`, `peak_rss_dest_kb` and `proc_scope` (not comparable across methods; see **Per process**);
- `verified`, `identical_to_s`, `caches_dropped`, `error`.

## Smoke run

**These numbers predate the fairness fixes.** rsync's times here leave out its copy of B0 (untimed then) and its fsync. Peak memory was the launcher's, and speeds always ran in ascending order. See the next section for the smoke run with the fixes.

October 22, 2026, 10:38 to 10:42 UTC, commit f442513 (clean tree), settings as in the smoke command above. The run took 3 minutes.
- All 30 method runs passed verification, and B0 was unchanged in both trials.
- The tbf shaping delivered 95.9% of the target rate as TCP payload in both directions: 0.479 Gbit/s at 0.5 and 1.917 at 2.
- Unthrottled, the link ran at 13.1 to 15.1 Gbit/s.
- Local disks wrote 1.63 GB/s and read 2.0 GB/s (dd, direct I/O).

The results are in `results/exp04-smoke/` (not committed).

At scale 5 (190 MB backups), fixed costs dominate:
- the staged methods' local capture takes about 1 s;
- pgdelta's Python hashing and verification take about 1.6 s.

So in the smoke run only the incremental backup and rsync beat the full copy at 0.5 Gbit/s, and pgdelta only did for `append`. The smoke run checks the tooling; it is not a result. The crossover question needs scale 70 and the slow speeds.

End-to-end time relative to the full copy (`report.py`; one run per cell):

| Trial | Method | 0.5 Gbit/s | 2 Gbit/s | unthrottled |
|---|---|---|---|---|
| uniform 1% | incremental | 0.50 | 1.07 | 1.09 |
| uniform 1% | pgdelta | 1.09 | 2.56 | 2.78 |
| uniform 1% | rsync_8k | 0.80 | 1.88 | 2.07 |
| uniform 1% | rsync_default | 0.78 | 1.88 | 2.00 |
| append 1% | incremental | 0.28 | 0.79 | 0.88 |
| append 1% | pgdelta | 0.92 | 2.43 | 2.70 |
| append 1% | rsync_8k | 0.63 | 1.57 | 1.69 |
| append 1% | rsync_default | 0.62 | 1.59 | 1.67 |

The full copy took 3.1 s at 0.5 Gbit/s and 0.97 to 1.0 s unthrottled, for 174 MB on the wire. For the uniform trial at 0.5 Gbit/s, rsync with 8 KiB blocks sent 58.6 MB and pgdelta 58.7 MB.

## Smoke run with the fairness fixes

**rsync's figures here are superseded.** This run took rsync's copy of B0 and its capture one after the other, with a second cache drop between them that evicted the fresh O, so rsync's `total_s` here is what is now `serial_total_s` plus a cold re-read of O. The pgdelta, full and incremental figures still stand. See the round-2 smoke run below.

October 22, 2026, 11:21 to 11:24 UTC, commit 24cef85 (clean tree, the same on both VMs). Settings: scale 5, `uniform` and `append` at 1%, 0.5 Gbit/s and unthrottled, one repeat, the default methods. The results are in `results/exp04-fixes-smoke/` (not committed).
- **All 20 method runs passed verification**, and B0 was unchanged in both trials.
- **Speed order:** shuffled per repeat and recorded: `0.5g, unthrottled` for uniform, `unthrottled, 0.5g` for append.
- **rsync prep:** timed at 0.47 to 0.51 s, for example `cp -a` of the 190 MB B0 in 0.39 s, then `sync -f` in 0.12 s. It is in `total_s`, and its 0.29 to 0.33 VM CPU-seconds on dest are in `cpu_dest_s`.
  - The transfer command line ends `--stats --fsync S/ rsync://10.128.0.13:8730/exp04/<method>/`.
  - The cache drop after the prep took 0.07 to 0.09 s on dest, against 0.03 to 0.06 s for the other methods. O is already synced by then, so the difference is evicting the freshly copied pages, not writeback.
- **Peak memory, now each tool's own** (GNU time). The start-up probe gave `/bin/true` 1,136 KiB on dest and 1,172 KiB on source, against 23,200 and 17,152 KiB through `wait4`, which is the launcher's high-water mark. Per tool:
  - pg_basebackup 8.5 to 9.4 MB (8.2 MB for `pg_basebackup --version` alone), where `wait4` had said 17 to 30 MB;
  - pg_combinebackup 7.0 MB;
  - rsync 7.4 to 7.7 MB sending, and 8.3 to 9.7 MB for the largest single process of the daemon, its generator and its receiver (not their sum; see **Per process**);
  - cp 2.3 to 2.4 MB and sync 1.6 to 1.8 MB;
  - pgdelta send 22 to 25 MB and receive 24 to 26 MB (Python).
- **WAL:** every backup's `pg_wal` held one 16,777,216-byte segment with 288 bytes of real WAL, so `wal_padding_bytes` was 16,776,928. That segment shows on the wire: for append at 0.5 Gbit/s, pgdelta sent 17.7 MB and incremental 5.75 MB. For `full`, `output_minus_wal_padding_bytes` was 172.5 to 172.7 MB, against 173.7 to 174.6 MB on the wire.
- **Remote-launch overhead:** 0.116 to 0.121 s per staged run.
- **Daemon logs:** one rsync daemon log and transfer log per method and speed, for example `r0-0.5g-rsync_8k-rsyncd-transfers.log` and `r0-0.5g-rsync_default-rsyncd-transfers.log`.

End-to-end time relative to the full copy (`report.py`, `total_s`, one run per cell); the in-place lower bound is in brackets:

| Trial | Method | 0.5 Gbit/s | unthrottled |
|---|---|---|---|
| uniform 1% | incremental | 0.51 | 1.11 |
| uniform 1% | pgdelta | 1.09 | 2.74 |
| uniform 1% | rsync_8k | 1.01 (0.85) | 2.61 (2.11) |
| uniform 1% | rsync_default | 0.94 (0.79) | 2.70 (2.19) |
| append 1% | incremental | 0.28 | 0.93 |
| append 1% | pgdelta | 0.90 | 2.66 |
| append 1% | rsync_8k | 0.75 (0.60) | 2.21 (1.70) |
| append 1% | rsync_default | 0.77 (0.61) | 2.24 (1.74) |

Counting the copy of B0 and its fsync, one after the other with the capture, moved rsync's end-to-end ratios up against the first smoke run: by 0.12 to 0.21 at 0.5 Gbit/s, and 0.5 to 0.7 unthrottled (different runs, one each). rsync_8k's 1.01 for uniform at 0.5 Gbit/s came only from that serialisation: with the copy beside the capture it would have been about 0.85. As before, this is scale 5, where fixed costs dominate; it checks the tooling, not the hypotheses.

## Smoke run with the round-2 fixes

October 22, 2026, 11:55 to 11:57 UTC, commit 4fc04d5 (clean tree, the same on both VMs). Settings: scale 5, `uniform` and `append` at 1%, 0.5 Gbit/s and unthrottled, one repeat, the default methods. The results are in `results/exp04-fixes-smoke/` (not committed; this run replaced the previous one there).
- **All 20 method runs passed verification**, and B0 was unchanged in both trials. Speed orders were shuffled and recorded as before: `0.5g, unthrottled` for uniform, `unthrottled, 0.5g` for append.
- **One cache drop per run**, for every method; no record has a second drop.
- **rsync's prep beside its capture.** In all 8 rsync runs, both parts started at offset 0.0 s of the `prep_capture` phase.
  - The prep (`cp -a` of the 190 MB B0, then `sync -f`) took 0.48 to 0.49 s, and the capture 1.01 to 1.08 s.
  - The pair's wall equalled the capture's to within 1 ms, so at scale 5 the copy of B0 adds nothing to `total_s`. In-place and end-to-end totals are equal here. `serial_total_s` is 0.48 to 0.49 s higher.
  - `total_s` matched max(prep, capture) + transfer in every rsync record.
- **O warm for rsync.** rsync's transfer took 0.17 to 0.21 s less than in the previous run, cell for cell (for example 1.315 against 1.504 s for uniform `rsync_8k` at 0.5 Gbit/s). Dest iowait during the transfer fell from 0.13 to 0.16 s to 0.01 to 0.02 s. That was the cold re-read of O that the second drop used to force.
- **Counter windows.** rsync's totals now hold two windows, like pgdelta's (`counter_windows` 2; `full` 1). `phase_floor` measured 0.04 to 0.06 CPU-seconds on the source per empty window and 0.00 on dest, where the snapshots are in-process reads. `prep_capture_cpu_dest_s` was 0.26 to 0.35 s.
- **`--fsync`** is in every rsync transfer's command line.
- **Peak memory** (GNU time, largest single process; probe: `/bin/true` 1,124 KiB on source and 1,152 KiB on dest, against 17,256 and 23,816 KiB through `wait4`):
  - pg_basebackup 8.4 to 9.3 MB;
  - pg_combinebackup 7.0 MB;
  - cp 2.3 to 2.4 MB and sync 1.7 to 1.8 MB;
  - rsync sending 7.4 to 7.7 MB, and the daemon's largest process 8.4 to 9.7 MB;
  - pgdelta send 22 to 25 MB and receive 24 to 26 MB.
- **Logs per method.** The source holds `r0-<speed>-<method>-capture.log` and its `.time` for each staged method, and each one's peak matches its own record: for uniform at 0.5 Gbit/s, 8,604, 8,816 and 8,384 KiB for pgdelta, rsync_8k and rsync_default. The rsync daemon logs are per method, as before.
- **WAL:** `wal_bytes` 16,777,216 in every record, with 288 bytes of real WAL (`wal_stream_bytes`). For `full`, `output_minus_wal_padding_bytes` was 172.5 to 172.7 MB against 173.7 to 175.0 MB on the wire, and `tool_reported.scope` names it.
- **Remote-launch overhead:** 0.120 to 0.125 s per staged run. At this scale the capture is the longer part, so its launch is on the critical path.
- **One unexplained byte count.** The uniform unthrottled `incremental` capture counted 54.9 MB from source to dest. Every other incremental capture of this dataset counted 46.6 to 47.3 MB: in this run, in the previous one, and in a 6-run probe afterwards (`results/exp04-r2-incr-probe/`, `full,incremental` only, 3 repeats). The increment on disk was the same 62,045,388 bytes.
  - That capture sent fewer packets than the previous run's (2,677 against 2,833), and dest returned about the same ACK bytes (356 against 365 KB), so the extra bytes don't look like backup data.
  - The source had retransmitted only 265 TCP segments since boot.
  - The cause is unknown. This code path didn't change in round 2. If it recurs in the sweep, per-phase TCP counters (`nstat` OutSegs and RetransSegs) would help find it.

End-to-end time relative to the full copy (`report.py`, `total_s`, one run per cell); the serial upper bound is in brackets:

| Trial | Method | 0.5 Gbit/s | unthrottled |
|---|---|---|---|
| uniform 1% | incremental | 0.51 | 1.09 |
| uniform 1% | pgdelta | 1.10 | 2.71 |
| uniform 1% | rsync_8k | 0.75 (0.91) | 1.86 (2.36) |
| uniform 1% | rsync_default | 0.73 (0.89) | 1.93 (2.43) |
| append 1% | incremental | 0.28 | 0.74 |
| append 1% | pgdelta | 0.92 | 2.19 |
| append 1% | rsync_8k | 0.56 (0.71) | 1.23 (1.64) |
| append 1% | rsync_default | 0.55 (0.70) | 1.24 (1.65) |

rsync's end-to-end totals fell by 0.62 to 0.81 s per run against the previous run. About 0.5 s of that is the prep, now hidden behind the capture, and 0.17 to 0.21 s is the warm O. At 0.5 Gbit/s the ratios fell by 0.19 to 0.26. The unthrottled ratios fell further, but the unthrottled full copy itself ran 0.97 s for uniform and 1.19 s for append, which moves them too. As before, this is scale 5, where fixed costs dominate; it checks the tooling, not the hypotheses.

## Smoke run with pgdelta's receiver beside its capture

October 22, 2026, 17:31 to 17:34 UTC, commit 8a486f6 (clean tree, the same on both VMs). Settings: scale 5, `uniform` and `append` at 1%, 0.5 Gbit/s and unthrottled, one repeat, all six methods (`--methods full,incremental,pgdelta,pgdelta_1thread,rsync_8k,rsync_default`). The results are in `results/exp04-parity-smoke/` (not committed).
- **All 24 method runs passed verification**, and B0 was unchanged in both trials. Speed orders: `0.5g, unthrottled` for uniform, `unthrottled, 0.5g` for append. Both pgdelta methods reported the switches and the prefetch switch they pin (`switches_ok`, `prefetch_ok`), and `pgdelta`'s workers were started with `spawn` on both ends.
- **The receiver started beside the capture.** In all 8 pgdelta runs, both parts of `receiver_capture` started at offset 0.0 s, after the run's one cache drop.
  - The receiver listened after 0.15 to 0.18 s (`receiver_start_s`) and the capture took 1.00 to 1.05 s, so the pair's wall equalled the capture's to within 1 ms: the receiver's start-up added nothing to `total_s`. `serial_total_s` is 0.15 to 0.18 s higher.
  - Before this change the same start-up sat inside the transfer timer.
- **The prefetch ran in every pgdelta run.** It read all of B0, 188,744,564 bytes in 992 files, in 0.26 to 0.27 s, with `posix_fadvise` and `O_NOATIME`, and finished (`stopped: finished`) 1.12 to 1.17 s after it started, before the sender connected. The cap was 16.4 GB, half of `MemAvailable`. The receiver's log says so, for example `prefetched 188744564 of 188744564 basis bytes (992 of 992 files) in 0.261 s, while waiting 1.139 s for the sender; stopped: finished`.
- **Hashing B0 got cheaper.** `hash_basis` was 0.25 to 0.29 s for `pgdelta` and 0.42 to 0.45 s for `pgdelta_1thread`. The round-2 smoke run's `pgdelta` was the same single-threaded engine with the receiver in the transfer and B0 cold: 0.66 to 0.72 s. Cell for cell against that run (different runs, one each), `pgdelta_1thread`'s transfer was 0.15 to 0.26 s shorter and its `total_s` 0.12 to 0.26 s shorter.
- **Cost on dest.** `receiver_capture_cpu_dest_s` was 0.18 to 0.30 s: the receiver's start-up, the prefetch and the capture's SSH client. The receiver's peak RSS was 28.6 to 30.7 MB, against 24 to 26 MB before. The likely cause is the prefetch's 8 MiB read buffer, held while it reads and freed when it stops; this run doesn't show when the peak was reached.

End-to-end time relative to the full copy (`report.py`, `total_s`, one run per cell); the serial upper bound is in brackets. Two cells were first printed 0.01 lower (1.04 and 0.74), because `report.py` rounded each ratio twice; it now rounds once, from the medians:

| Trial | Method | 0.5 Gbit/s | unthrottled |
|---|---|---|---|
| uniform 1% | incremental | 0.51 | 1.15 |
| uniform 1% | pgdelta | 0.95 (1.00) | 2.25 (2.41) |
| uniform 1% | pgdelta_1thread | 1.05 (1.10) | 2.67 (2.83) |
| uniform 1% | rsync_8k | 0.74 (0.90) | 1.93 (2.44) |
| uniform 1% | rsync_default | 0.73 (0.88) | 1.91 (2.43) |
| append 1% | incremental | 0.29 | 0.92 |
| append 1% | pgdelta | 0.75 (0.79) | 2.15 (2.31) |
| append 1% | pgdelta_1thread | 0.83 (0.88) | 2.44 (2.59) |
| append 1% | rsync_8k | 0.54 (0.69) | 1.48 (1.99) |
| append 1% | rsync_default | 0.54 (0.69) | 1.51 (2.01) |

As before, this is scale 5, where fixed costs dominate; it checks the tooling, not the hypotheses. pgdelta's transfer still holds fixed costs that don't overlap the capture: the sender's start-up and its pool, and the receiver's pool, which can only start once HELLO has brought the key.

## Smoke run after the review of the receiver change

October 22, 2026, 18:07 to 18:10 UTC, commit 5b20299 (clean tree, the same on both VMs), with the same settings and order seed as the previous run. The results are in `results/exp04-review-smoke/` (not committed). It checks the new per-run measurement of the prefetch against the timed phases.
- **All 24 method runs passed verification**, and B0 was unchanged in both trials. Speed orders were the same as in the previous run. Both pgdelta methods reported the switches they pin (`switches_ok`, `prefetch_ok`).
- **No pgdelta run read B0 off the clock.** `prefetch_untimed_s` was 0 in all 8 pgdelta runs. Each prefetch read all of B0 (188,744,564 bytes) in 0.26 to 0.27 s and ended 0.58 to 0.63 s before `receiver_capture` did. The untimed gap between the two phases (`untimed_gap_s`) was 0.117 to 0.124 s.
- **The clocks agree.** The prefetch started 0.144 to 0.157 s after `receiver_capture` began, just after the receiver part's 0.149 to 0.161 s (when the harness saw the log line). The sender connected 0.146 to 0.157 s into the transfer.
- **Otherwise as before.** The receiver listened after 0.15 to 0.16 s, against a capture of 1.00 to 1.05 s. `hash_basis_s` was 0.26 to 0.29 s for `pgdelta` and 0.42 to 0.43 s for `pgdelta_1thread`. rsync's prep took 0.48 to 0.50 s against a capture of 1.01 to 1.04 s. Remote-launch overhead was 0.119 to 0.125 s per staged run.

End-to-end time relative to the full copy (`report.py`, `total_s`, one run per cell); the serial upper bound is in brackets:

| Trial | Method | 0.5 Gbit/s | unthrottled |
|---|---|---|---|
| uniform 1% | incremental | 0.51 | 1.15 |
| uniform 1% | pgdelta | 0.95 (1.01) | 2.24 (2.41) |
| uniform 1% | pgdelta_1thread | 1.05 (1.10) | 2.67 (2.83) |
| uniform 1% | rsync_8k | 0.74 (0.90) | 1.94 (2.45) |
| uniform 1% | rsync_default | 0.74 (0.90) | 1.92 (2.43) |
| append 1% | incremental | 0.28 | 0.75 |
| append 1% | pgdelta | 0.75 (0.80) | 1.77 (1.90) |
| append 1% | pgdelta_1thread | 0.84 (0.89) | 1.97 (2.10) |
| append 1% | rsync_8k | 0.54 (0.69) | 1.21 (1.62) |
| append 1% | rsync_default | 0.55 (0.70) | 1.22 (1.63) |

The 0.5 Gbit/s and uniform unthrottled ratios are within 0.01 of the previous run's (the serial bounds within 0.02). The append unthrottled ratios are lower because this run's full copy took 1.19 s there, against 0.96 s before. As before, this is scale 5, where fixed costs dominate; it checks the tooling, not the hypotheses.

## Limitations

- **Bandwidth only, no latency.**
  - tbf caps the rate; the round-trip time stays at the zone's roughly 0.1 ms.
  - Real slow links usually also have tens of milliseconds of RTT. That would hurt protocols with round trips (pgdelta's leaf requests, rsync's checksum exchange) more than a streaming copy.
  - `tc netem` delay would be the next variant.
- **Interface counters see TSO/GSO super-packets.** virtio_net counts each segmentation-offload packet once, with one set of headers, so `/proc/net/dev` undercounts the per-segment TCP/IP/Ethernet headers that really cross the wire (about 3 to 4% at MTU 1460). tbf accounts for those headers, which is why a shaped rate R carries about 0.96 R of payload. The counters are independent of the tools, but they are not exact wire bytes.
- **VM-wide CPU** includes everything on the VM during the phase: the agent, the SSH multiplexer, the guest agent and the kernel. The idle background is about 0.01 CPU-seconds per second on each VM, 1% of one vCPU.
- **Remote command overhead** of about 60 ms is inside every remote phase (staged captures, pgdelta's sender start, rsync). Local phases (full, incremental) have none. It is reported per run, and `total_no_ssh_s` takes it out.
- **The rsync daemon is started outside the timer**, as a standing service would be. pgdelta's receiver is started inside the timer, in `receiver_capture` beside the capture, so its start-up counts only where it outlasts the capture.
- **rsync's copy of B0 and pgdelta's receiver start-up overlap the capture.** That is what a real refresh across two machines would do, but one that had to finish them before taking S would take `serial_total_s`. Both are recorded. pgdelta's `serial_total_s` still has the prefetch the receiver did while it waited, so it is not the old receiver-in-the-transfer arrangement, which read B0 cold.
- **pgdelta's prefetch depends on the wait.** It reads B0 only while the receiver waits for the sender, so how much of B0 is warm depends on how long the capture takes against how fast B0 reads. `prefetch_bytes` and `prefetch_stopped` say how much was read in each run.
- **Part of pgdelta's prefetch can be untimed.** Where the prefetch outlasts the capture, it reads on during the untimed gap in which the counters are read, about 0.12 s. `prefetch_untimed_s` measures that per run (see **CPU** above).
- **Incremental reads B0 cold.** It is the only method with a cold read of B0 on its critical path (`pg_combinebackup`, after the capture). It has no wait at the destination to warm B0 in, since its capture runs there. See EXP-04's note.
- **One TCP stream** for the link test. pg_basebackup uses two connections (data and WAL), pgdelta and rsync one each.
- **The source is otherwise idle.** No workload runs during the backups. Autovacuum stays on, as in EXP-01, and a long sweep gives it time to run: at the default 1% fractions no table reaches its threshold, but a 10% fraction would trigger an autoanalyze. `trials/*.json` has `pg_stat_user_tables` after the workload and at the end, so a run can be checked.
- **Each capture adds a checkpoint and a WAL segment to the source.** So S differs from run to run by pg_control, the backup label and a new 16 MiB WAL segment, which is zero-padded on disk. Every method still sees the same data pages. But the staged methods (pgdelta, rsync) send that whole segment, about 16.8 MB per run, where `full` and `incremental` stream only the few KB of real WAL. That's a fixed per-run cost of the staged design, recorded per run as `wal_bytes` and `wal_padding_bytes` (see **WAL** above).
- **Plain format, default tablespace, 8 KiB blocks**, PostgreSQL 17.11 only.

## Tests

`python -m unittest tests.test_exp04_network -v` covers the pure logic without a VM:
- `/proc/net/dev` and `/proc/stat` parsing and deltas;
- speed parsing and the tc rates;
- seeded method orders, and seeded per-repeat speed orders recorded in the trial and run records;
- tree digests, comparisons, the independence and B0 checks;
- phase totals, with and without the remote-launch overhead;
- the CSV row and writer;
- rsync `--stats` parsing of a real daemon-push output;
- the agent's result line, `remove` guard, `run` and a loopback bulk transfer;
- peak RSS under a GNU time stand-in, for `Child` and `agent.py run`, and that `proc_scope` says %M is the largest single process;
- process-group signalling that spares GNU time: `Child.signal` with `group_members`, `os.kill` and `os.killpg` patched (SIGTERM reaches the tool's group but not GNU time, SIGKILL goes to the whole group, no `/proc` or no GNU time falls back to the group), and, on Linux only, a SIGTERM'd tool under the GNU time stand-in whose report is still written (skipped on macOS; run it on a VM);
- WAL bytes, stream bytes and padding from a manifest and a tree;
- the critical-path launch overhead of a concurrent phase, and its serial and in-place views;
- stubbed orchestrator runs:
  - one cache drop per method run, after the rsync daemon starts and before the timed phases;
  - rsync's prep (`cp -a`, then `sync -f`) and its capture run at the same time: each waits on a barrier for the other;
  - rsync's end-to-end total is max(prep, capture) + transfer, for a prep longer and shorter than the capture, with the serial upper bound and the in-place lower bound beside it;
  - rsync's totals hold as many counter windows as pgdelta's, and the phase floor is measured with empty phases;
  - each staged method's capture log (and pgdelta's stats log) is its own;
  - full and incremental name `output_minus_wal_padding_bytes` as their byte reference;
  - rsync's transfer has `--fsync`;
  - each rsync method gets its own daemon log and configuration;
  - `pgdelta_1thread` passes EXP-02's engine (`--jobs 1`, gap-fill, sequential) and `pgdelta` EXP-06's (`--jobs 4`, copy-patch, pipelined), parsed by pgdelta's own command line; `--rebuild`, `--verify` and `--prefetch-basis on` go only to the receiver; the reported switches and prefetch statistics are recorded and checked;
  - pgdelta's receiver and its capture run at the same time after the one cache drop (each waits on a barrier for the other), and the sender starts only after the receiver listened; pgdelta's total is max(receiver start, capture) + transfer, for a receiver slower and quicker than the capture, with the serial upper bound beside it; the receiver's record (its whole life) is in the transfer phase;
  - a capture that fails or raises, a transfer that raises, and a receiver that exits before listening each fail the run and leave no receiver running;
  - how long pgdelta's prefetch read B0 in the untimed gap between its two phases, recorded per run and in the CSV (`measure.prefetch_untimed`: none, part of the gap, the whole gap, older records without `ended_utc`);
  - `both()` waits for the source-side call when the local call fails or is interrupted, and when SIGTERM's KeyboardInterrupt arrives, even twice, while it awaits the source side. The SIGTERMs are real signals to the main thread, handled by `run_exp04._terminate`; these tests fail with the `both()` from before that fix;
- `remote.py`'s config and instance parsing;
- the source cluster's EXP-01 spec and pg_hba lines;
- the report's medians and crossover, the serial rows (rsync and pgdelta) with their caveat, and in-place rows, and the labelled overhead-free view. The crossover compares the medians, not rounded ratios, and ratios are rounded once, for display. Runs whose prefetch read B0 between timed phases are listed.

pgdelta's prefetch itself is tested in `tests/test_pgdelta_prefetch.py`.
