# Experiment 4: refresh methods over throttled links

## Question

On two real machines, at what link speed does pgdelta stop beating a full copy? EXP-02 counts bytes and CPU on one machine. This experiment adds the network: two VMs, a link shaped to several speeds, and end-to-end time for each method, with bytes on the wire counted at the network interface rather than taken from each tool.

## Setup

- **Machines:** `pgd-source` and `pgd-dest` (see [infra/gcp/](../../infra/gcp/README.md)).
  - `n2-standard-8` with Debian 12 and PostgreSQL 17 from PGDG, rsync 3.2.7, Python 3.11 and GNU time 1.9 (Debian package `time`, for each tool's peak memory). pgBackRest from PGDG and xfsprogs are installed for T11 and T3 (d).
  - Local NVMe SSDs striped at `/data`: 4 per VM, about 1.6 GB/s writes and 2.0 GB/s reads with direct I/O. The design asked for 2, but the zone had no stock for that.
  - Both VMs are in one zone and talk over internal addresses. MTU 1460, virtio_net, 8 queues. A single TCP stream between them runs at 13 to 15 Gbit/s unthrottled.
- **Control:** `run_exp04.py` runs on `pgd-dest`, the receiving side. It drives `pgd-source` over one multiplexed SSH connection with a key made on `pgd-dest` for this purpose.
  - `agent.py` runs on both VMs for counters, `tc`, cache drops, link tests and tree digests.
  - `source_cluster.py` runs EXP-01's cluster and workload code on the source.
  - `parcopy.py`, EXP-11's bound, runs its receiver on dest and its sender on the source (`python -m experiments.network.parcopy`).
  - `durability_audit.py`, EXP-11's syscall audit, checks `strace` traces of dest's timed commands against the durability definition.
  - `remote.py`, run from the Mac, deploys the code, starts the orchestrator under `nohup` and fetches the results.
- **Code on the VMs:** a tarball of the working tree's tracked files, unpacked at `~/pgd`, with `DEPLOYED.json` (commit, dirty flag, tree hash, and `pgdelta_since_exp09`: `git diff` of `pgdelta/` from EXP-09's deployed commit `b77f33d` to the deployed tree, its files, line counts, SHA-256 and text, which EXP-11's Setup asks for and `analysis/exp11.py` prints beside the drift gates). A run refuses to start if the two VMs hold different trees.

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
   2. Untimed set-up: pgdelta's stats directory on the source; the rsync daemon on dest. For the optional methods of T3, T8a and T11 also B, a copy of B0, and a clean stop of the source (see [their section](#methods-for-t3-t8a-and-t11)).
   3. `sync; echo 3 > /proc/sys/vm/drop_caches` on both VMs, once, recorded with timestamps in `cache_drop`.
   4. The timed phases (below).
   5. Untimed verification.
   6. Delete the outputs (and restart the source, if it was stopped).

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
- `prep_s`, `capture_s`, `prep_capture_s` (the pair, rsync only), `receiver_start_s` and `receiver_capture_s` (the pair, pgdelta only), `transfer_s`, `combine_s` and `restore_s` (pgBackRest) are the phases and parts.

## Methods for T3, T8a and T11

These were added on October 23, 2026, after the owner's decisions that closed T1 ([DECISIONS](../../docs/DECISIONS.md#decisions-of-october-23-2026-t1-closed)): B may be patched in place, and T11, T8a and T3 run next. None is in the default `--methods`; name them to run them.
- **Each pgdelta method keeps EXP-06's engine and changes one thing.** That is 4 jobs on both ends and `--prefetch-basis on`, pinned by flags in `measure.PGDELTA_ENGINES` like `pgdelta`'s. Unlike `pgdelta` and `pgdelta_1thread`, which are left as they were, every new one also pins the receiver's `--fsync`, so a change of its default can't move them.
- **The pre-run check asks for what the selected methods use.** It is `measure.pgdelta_required_options(methods)`, the union of their flags: the default methods need what they always did, T3's add the receiver's `--fsync` and `--in-place`, and T8a's add the sender's `--source-kind` and an importable `pgdelta.datadir` on both VMs. A run refuses to start on a pgdelta without them. These options are being added to pgdelta separately; the harness is written against that contract.
- **Each record says what pgdelta reported.** `tool_reported.modes` holds `in_place`, `fsync_mode` and `source_kind`, wherever pgdelta's statistics keep them. `modes_ok` says whether they are the pinned ones, and is None while pgdelta reports none of them. `fsync_wait_seconds` is there too.
  - **The fsync mode is the one in effect.** pgdelta's receiver reports `fsync`, the mode asked for, and `fsync_effective`, the one in effect: `writeback` falls back to `main` where `sync_file_range` is missing or refused. `modes.fsync_mode` (and the CSV's `fsync_mode`) is `fsync_effective`, and `tool_reported.fsync_requested` is `fsync`, so a `pgdelta_fsync_wb` run that fell back shows `main` and fails `modes_ok`. Only mode names count: the `fsync` among the timings is seconds. `switches_ok` now compares only the switches a method pins, so new keys in `switches` don't fail it.

| Method | For | What it changes | Timed phases | Verification (untimed) |
|---|---|---|---|---|
| `pgdelta_gapfill_seq` | T3 (a) | `--rebuild gap-fill --verify sequential` | as `pgdelta` | as `pgdelta` |
| `pgdelta_seq` | T3 (a), (c) | `--rebuild copy-patch --verify sequential` | as `pgdelta` | as `pgdelta` |
| `pgdelta_fsync_wb` | T3 (b) | `--fsync writeback` | as `pgdelta` | as `pgdelta` |
| `pgdelta_fsync_worker` | T3 (b) | `--fsync worker` | as `pgdelta` | as `pgdelta` |
| `pgdelta_inplace` | T3 (e) | `--in-place --fsync main`: refreshes B, a copy of B0, with no `--output` and no `--rebuild` | as `pgdelta` | as `pgdelta`, on B; no in-place leftovers |
| `incremental_cfr` | T3 (d) | `incremental`, combined with `pg_combinebackup --copy-file-range` instead of `--copy`: on XFS it clones B0's unchanged blocks into O, on ext4 it copies in the kernel | as `incremental`: **capture**, **combine** | as `incremental` |
| `pgdelta_direct` | T8a | the sender reads the cleanly stopped source (`--source-kind datadir`); today's write path, a new O | **receiver_transfer**, one phase: **receiver_start**, then at once **transfer** | O's files byte-identical to the source's selected files; O starts and answers a query; no inode shared with B0 |
| `pgdelta_direct_inplace` | T8a | as `pgdelta_direct`, refreshing B in place | as `pgdelta_direct` | as `pgdelta_direct`, on B; no in-place leftovers |
| `pgbackrest_zst`, `pgbackrest_none`, `pgbackrest_block` | T11 | `pgbackrest restore --delta` of B from a repository on the source | **restore** | B starts, replays the archive, and its per-table digest equals the source's |

`pgdelta`, run in the same invocation, is each T3 arm's baseline: copy-then-patch, pipelined, fsync in the main loop, a new O.

**Untimed set-up, before the cache drop.** It is recorded in each run's `preparation`, and its wall time is the CSV's `untimed_setup_s`.
- **The in-place methods and pgBackRest patch B, a copy of B0.** The copy is `cp -a --reflink=never B0 B`, then `sync -f B`, into the method's output path. B stands in for the old copy that a real refresh already has, and B0 stays untouched for the next method, as before. B is cold when the timers start.
  - **B shares no block with B0.** Since coreutils 9.0 (Debian 12 has 9.1), `cp` defaults to `--reflink=auto`, which on dest's XFS would make B a clone of B0: every page then written into B would be a copy-on-write unshare of one of B0's blocks, which a real old copy doesn't pay. So the copy says `--reflink=never` ([EXP-10](../../docs/experiments/EXP-10-write-path.md)), and FIEMAP (`measure.shared_extents`) then checks that no extent of B is flagged shared; one that is fails the run before the cache drop. The check is in the step's `shared_extents`.
- **pgdelta_direct\* stops the source cluster cleanly** with `source_cluster.py stop-clean`: `pg_ctl stop -m fast`, after which `pg_controldata` must say `shut down`. The cache drop comes after it, so the sender reads the source's files cold.

**T3: staged like pgdelta.** The receiver starts beside the capture of S, prefetches B0 while it waits, and then comes the transfer. Totals, serial bounds and records are pgdelta's.
- **`pgdelta_inplace`'s receiver** is `pgdelta receive --basis B --in-place --listen ... --jobs 4 --verify pipelined --prefetch-basis on --fsync main`. While it runs, pgdelta writes `B/pgdelta-incomplete.json` and keeps `global/pg_control` under another name, so PostgreSQL refuses to start on a half-done B. Its receipt is `B.pgdelta-receipt.json`, beside B.
- **Its verification is pgdelta's, on B:** `pg_verifybackup`, B byte-identical to S, and no inode shared with B0. On top of that, `measure.in_place_leftovers`: no marker, no entry named after pgdelta, no other `global/pg_control*` file, and `global/pg_control` back in place. The comparison with S would catch any other leftover.

**T8a: the sender reads the stopped source directly.**
- **One timed phase, `receiver_transfer`, in two steps.** **receiver_start** runs on dest, from launching `pgdelta receive --basis B0 --output O --listen ...` until its log says it is listening. The **transfer** follows at once: `pgdelta send --source-kind datadir --source /data/exp04/src/data` on the source, until both ends exit. Each step's wall and start offset are in the phase's `steps`, and the CSV's `receiver_start_s` and `transfer_s` are those walls. `total_s` is the phase's wall, their sum to within microseconds. Without a capture, there is no serial bound.
- **No untimed gap.** The receiver prefetches B0 from when it listens until the sender connects. Two timed phases would put the counter reads, about 0.12 s with two SSH calls, between the two, and the receiver would prefetch B0 there, off the clock, in nearly every run; with no capture to overlap, nothing forces that split. So the counters are read once around both steps (`counter_windows` 1), the sender starts as soon as the receiver listens, and `prefetch_untimed` is empty for these methods. The receiver's record, with its whole life, is in the phase's commands.
- **Verification, while the source is still stopped:**
  - The agent's `datadir-digest` runs `python -m pgdelta.datadir list --sources` on the source's data directory, which prints the files pgdelta's datadir mode selects (PostgreSQL's base-backup exclusions applied), each with the file whose bytes it sends, and hashes only those, from that file. The two differ for an unlogged relation's main fork, which pgdelta sends with its init fork's bytes, the empty relation that recovery's reset would leave, since the copy starts without recovery. The record's `verification.read_from` lists them. A pgdelta whose `list` has no `--sources` lists paths only, each hashed as itself. EXP-01's tables are all logged, so EXP-04's data has no such file.
  - O's regular files must be exactly those files, with the same SHA-256. `identical_to_s` reports it, `capture_bytes` holds the selected files' bytes, and `wal_bytes` the WAL in O.
  - Only then is O (or B) started on dest (below). It must answer `SELECT count(*) FROM pgbench_branches`, and is stopped again.
  - B0 must be unchanged.
- **The source is restarted after every pgdelta_direct\* run, whatever happened:** a failed transfer, a failed verification, or even a failed clean stop. `source_cluster.py start` uses the options `setup` started it with, and leaves a running cluster alone. The result is in the record's `source_restart`. If the source can't be restarted, the trial stops with an error, since every later method needs it. Only that trial stops: the next one builds a new source cluster and runs as usual.

**Starting an output on dest** (pgdelta_direct\* and pgbackrest\_\*):
- The command is `pg_ctl start -o '-p 5433 -k /data/exp04/vs -c listen_addresses= -c archive_mode=off'`, with `LC_ALL=C` and no `PG*` variables in its environment.
- The settings override the output's `postgresql.conf`, which is the source's: it listens on the source's address, which dest doesn't have, and in a pgBackRest trial it would archive into the source's repositories.
- It is stopped with `pg_ctl stop -m fast`, then `immediate` if needed. **If it still runs after both,** the run fails and its data directory is left in place, never removed under the running server: not by that run's clean-up, nor by a later run of the same method, nor by the trial's clean-up. The record's `output_stuck` says so. For the rest of the invocation, every method that would start an output (pgdelta_direct\*, pgbackrest\_\*) or use that path fails at once, before its set-up, since the server still holds port 5433; the other methods run as usual.
- A run refuses to start if a server still holds port 5433 in `/data/exp04/vs` (from the socket's lock file, whose PID still runs), left by an earlier run that was killed, when it selects a method that starts an output.
- `output_start_s` is the time from `pg_ctl start` until the server answered, recovery included.

**T11: pgBackRest from an existing repository, the backup counted as already paid.**
- **Trial set-up, untimed, before B0, and only when a pgbackrest method is selected.** On the source, under `/data/exp04/pgbackrest/`:
  - a `pgbackrest.conf` with one stanza per selected variant, named after the method, each with its own repository (`repo1-path=.../repo-<method>`) and `process-max=4`;
  - `stanza-create` for every stanza, before archiving starts, since each WAL file goes to all of them;
  - `archive_mode = on`, with an `archive_command` that runs `pgbackrest archive-push` for every stanza in turn, added to `postgresql.conf` by `source_cluster.py archive`, which restarts the source once (a clean stop, then a start) and checks that the settings are in effect;
  - `check` for each stanza.
- **Its counterpart on dest:**
  - a `pgbackrest.conf` with the source as repository host (`repo1-host` its address, `repo1-host-user` the SSH user, `repo1-host-config` the source's configuration), and a stanza section per variant with its `repo1-path` and the variant's options that act where the command runs (`measure.PGBACKREST_DEST_OPTIONS`: `compress-level-network=0` for `pgbackrest_none`);
  - `cmd-ssh` set to a wrapper that runs `ssh -F /data/exp04/pgbackrest/ssh_config`, an SSH configuration of its own with the harness key (`--ssh-key`, `IdentitiesOnly`, `BatchMode`, `StrictHostKeyChecking accept-new`). `~/.ssh/config` is left alone.
  - `pgbackrest info` for each stanza on dest proves that the repository is reachable without a prompt.
- **One full backup per variant, untimed,** after the workload and its `CHECKPOINT`: `pgbackrest --stanza=<variant> --type=full backup`, with `start-fast=y`. `info --output=json` gives its size and repository size, in `trials/<trial>.json` (`pgbackrest.backups`) and each run's `tool_reported.backup`.
- **Timed, on dest:** **restore** is `pgbackrest --config=... --stanza=<variant> --delta --process-max=4 --pg1-path=B restore`. Its 4 processes each reach the repository over SSH, where pgBackRest turns SSH compression off. With `compress-type=none` and a remote repository, pgBackRest would still compress what it sends itself, at `compress-level-network` (default 3), so `pgbackrest_none` sets it to 0 on dest, where the restore and the recovery's `archive-get` run, and is uncompressed on the wire, as EXP-08 registers. The zst variants send their files as stored. pgBackRest reads every file of B to compare checksums, and fetches what differs.
- **Verification, untimed:**
  - B is started on dest (above). Recovery fetches WAL with the `restore_command` pgBackRest wrote (`archive-get`, over SSH to the source), runs to the end of the archive, and promotes. The harness waits until `pg_is_in_recovery()` is false, takes B's per-table digest, and stops it.
  - **The WAL that recovery fetches counts in pgBackRest's wire bytes** (EXP-08), as pgdelta's WAL travels inside its timed transfer. Both VMs' counters are read around the start, untimed, into `verification.recovery` (with `wal_files_restored`, the server log's `restored log file` lines), and those bytes are added to the run's wire totals: `wire_s2d_bytes` is the restore plus the recovery, and `recovery_wire_s2d_bytes` / `recovery_wire_d2s_bytes` hold the recovery's part, so the restore alone is the difference. Wall time and CPU stay the restore's (`totals.wire_bytes_scope` says so). The window also holds the start, the digest query (on a Unix socket) and the stop, which use no link, and the counter reads' own SSH traffic, as every window does.
  - **How much WAL that is depends on the run order.** Recovery replays to the end of the archive, and every staged capture before it in the trial (pgdelta's and rsync's `pg_basebackup` S, the T3 methods) ends a WAL segment that the source archives to every stanza. So a restore later in the trial fetches more segments, about 16 MiB each for `pgbackrest_none` and nearly nothing compressed for the zst variants, whose segments are mostly zeros. `recovery_wal_files` shows it per run; compare the recovery's bytes across orders only with it in view.
  - **The per-table digest** (`measure.logical_digest_sql`) covers EXP-01's tables: `pgbench_accounts`, `pgbench_branches`, `pgbench_tellers`, `pgbench_history` and `events`. For each, it takes the row count and an md5 over its rows in primary-key order, in two levels: per block of 65,536 key values, then over the blocks. `pgbench_history`, which has no key, hashes its rows sorted. The session runs in UTC with ISO dates, so a row's text doesn't depend on settings.
  - **The source's digest is taken once, after the trial's last method,** beside the row-level ground truth and for the same reason: a scan of the source earlier would set hint bits and change its pages under the other methods. Until then each pgBackRest run's status is `pending` (`logical_match` empty). At the end it becomes `pass` or `fail`, and its record and the CSV are rewritten (`trials/<trial>.json` `logical_check`).
- **What pgBackRest reports** (`tool_reported.restore`): from its console log and its detailed file log (kept as `<method>-pgbackrest-restore-file.log`), the restore size, the file total, the files that matched B, whether it completed, and its statistics line if it printed one. pgBackRest reports no network bytes; the wire columns are the measure.

| Variant | Stanza options |
|---|---|
| `pgbackrest_zst` | `compress-type=zst` |
| `pgbackrest_none` | `compress-type=none`, `compress-level-network=0` (on dest too: no compression on the network either) |
| `pgbackrest_block` | `compress-type=zst`, `repo1-bundle=y`, `repo1-block=y` (block incremental) |

**Archiving stays in pgBackRest runs.** Only a run with a pgbackrest method configures pgBackRest and turns archiving on, and only from its own trial set-up; every trial builds a fresh source cluster, and the repositories are removed with it. `run.json` `pgbackrest` records the configurations, the archive command and that archiving was on (it is null in other runs). Inside such a run, archiving does touch the other methods: each staged capture ends a WAL segment, which the source then pushes to every stanza in the background. To keep T3's and T8a's numbers clean, run T11 in its own invocation.

**Different speeds per method.** T3's arms run unthrottled only, while T8a's run at 2, 5 Gbit/s and unthrottled. `--method-speeds METHODS=SPEEDS` (repeatable; methods joined with `+`) limits the named methods to some of `--speeds`; the others run at every speed, and a speed left with no method is skipped. `run.json` records `method_speeds` and `methods_by_speed`. Separate invocations work too, but each builds its own trial and B0.

**ext4 or XFS** (T3 (d)): dest's `/data` is ext4 unless [`infra/gcp/reformat_dest_xfs.sh`](../../infra/gcp/README.md) has made it XFS with reflinks, which destroys everything on it. `run.json` records each VM's `data_mount` and, on XFS, `data_reflink`. Run the same T3 methods before and after.
- **What clones on XFS, and what doesn't.** rsync's timed prep keeps `cp -a`'s default, so on XFS it clones B0, as T3 (d) means it to; pgdelta's copy-then-patch clones through `copy_file_range`; `incremental_cfr` combines with `pg_combinebackup --copy-file-range`, which clones too (`incremental` keeps `--copy`). The untimed copies of B0 for the in-place methods and pgBackRest never clone (above).
- **Which kind of copy each output is** is measured, not assumed: every verification records the output's `shared_extents` from FIEMAP after the run (CSV `output_shared_bytes`, the bytes in extents shared with another file, which can only be B0). It is 0 on ext4, and empty where FIEMAP isn't available.

## Methods for T2

[EXP-11](../../docs/experiments/EXP-11-full-copy-baseline.md) asks how fast a durable full copy can go on these VMs and whether v1's path still beats it. Its arms are pinned in `measure`. None is in the default `--methods` except `full`.

| Method | Tier (`measure.TIERS`) | What runs | Source | Timed phase |
|---|---|---|---|---|
| `full` | `omni_today` | `pg_basebackup -Fp -X stream -c fast --manifest-checksums CRC32C --verbose` | running | **transfer** |
| `full_nosync` | `diagnostic` | `full` with `--no-sync`: not durable, the durability checks' negative control | running | as `full` |
| `full_syncfs` | `released` | `full` with `--sync-method=syncfs` | running | as `full` |
| `full_wb` | `host_tuned` | `full` with dest's `vm.dirty_background_bytes` at 64 MiB for the run | running | as `full` |
| `probackup8`, `probackup4` | `released` | `pg_probackup catchup -b FULL --stream --temp-slot -j 8` (or 4) on dest, over SSH, then `sync -f` of the output | running | **catchup_sync**: steps **catchup**, then **sync** |
| `parcopy8`, `parcopy4` | `bound` | `experiments/network/parcopy.py`: 8 or 4 TCP streams of 64 MiB ranges, write-back every 8 MiB | stopped cleanly | **receiver_transfer**, as `pgdelta_direct*` |
| `pgdelta_direct_inplace` | `v1` | T8a's arm, unchanged | stopped cleanly | as T8a |
| `pgdelta_direct_inplace_j8` | `v1_8_jobs` | the same with `--jobs 8` on both ends | stopped cleanly | as T8a |

**`run_exp04.py`'s options for T2**, all new but `--manifest-checksums`, whose default is unchanged (each is described below where it acts):

| Option | Default | What |
|---|---|---|
| `--manifest-checksums CRC32C` | SHA256 | required whenever a T2 arm is selected (rule 7) |
| `--extension-repeats N`, `--extension-methods M,...` | 0, none | EXP-11's extension (A9): N more unthrottled repeats of those methods after the main block; the two go together, and the methods must be in `--methods` |
| `--warm-source-repeats N`, `--warm-source-methods M,...` | 0, none | the warm-source block, after the extension: the source's cache read rather than dropped |
| `--idle-noise-windows N` | 0 | check (a)'s idle noise measured at the start: the largest `Dirty` + `Writeback` increase over N idle windows of `--idle-seconds` |
| `--dirty-noise-mib X` | 0 | check (a)'s idle noise as the smoke run measured it; the limit is the largest of 4 MiB, this and the windows' |
| `--pg-probackup PATH` | `/usr/local/bin/pg_probackup-17` | pg_probackup on both VMs; its directory is catchup's `--remote-path` |
| `--probackup-byte-diagnostic every\|first_repeat` | `every` | `probackup*`'s byte diagnostic in every run, or each trial's first repeat only (the plan's first cut) |
| `--syscall-audit` | off | every command of every timed phase on dest runs under `strace`, and each record holds its audit; the times then mean nothing |
| `--audit-filesystem PATH` | `/data` | the outputs' mount point, which a `syncfs` of it covers |

**New commands.**
- `agent.py`: `blast-send`/`blast-recv --streams N` (the multi-stream link test); `durable-write-test DIR --writers 4 --bytes B --writeback-bytes 8M` (the durable write floor); `read-test PATH --readers N --exclude pg_wal [--sync]` (the cold read floor, and one reader for a file); `sysctl get NAME...` and `sysctl set NAME=VALUE...` (through `sudo -n`); `snapshot` now reads `/proc/meminfo`'s `Dirty` and `Writeback` and `/proc/diskstats`; `env` records `vm.dirty_*` and the pg_probackup version.
- `source_cluster.py backup-mode-probe --repeats 10`: the backup-mode bracket, `pg_backup_start('probe', true)` and `pg_backup_stop(false)` on the idle source, its cache dropped before each pair.
- `python -m experiments.network.parcopy receive --output O --listen IP:PORT --stats-json F --streams N --writeback-bytes B` on dest, and `send --source D --pg-controldata BIN --connect IP:PORT --stats-json F --streams N --chunk-bytes C` on the source. Exit status 0, 2 for a refused input (a running cluster, a non-empty output, a stream-count mismatch), 3 for a failed copy.
- `durability_audit.py check --scope O TRACE...`, `run --trace F --scope O -- ARGV...` and `summary [--json] RUN_DIR...` (below).
- `analysis/exp11.py` (below, "Reading a T2 run").

**How each arm runs and is checked.**

- **One timed phase per arm,** one counter window, so no write-back can happen off the clock between two phases.
- **`--manifest-checksums CRC32C` is required.** It still defaults to SHA256, EXP-04's setting, and a run that selects any T2 arm (`measure.T2_METHODS`) refuses to start without CRC32C, in `parse_args` and in `execute`.
- **Every pg_basebackup arm runs with `--verbose`, `full` included,** and never `--progress`. Its output comes through a pipe that a thread of the orchestrator reads, stamping each line with the seconds since the launch as it arrives (`Child(stamp_lines=True)`; no extra process), with `LC_MESSAGES=C`. The timed phase ends when the command exits: a process that outlives it holding the pipe (an ssh pg_probackup started, say) is waited for after the window, and that wait is the command's `reader_wait_seconds`. `measure.basebackup_phases` splits the run into checkpoint, stream, WAL end, sync and manifest rename. The backup-mode adder (`backup_mode_adder_s`, EXP-11's `R_adj`) is the checkpoint wait plus the time from the WAL end point to the start of the sync.
- **Every pg_basebackup arm's output is started** after `pg_verifybackup`, its bytes counted first: on port 5433, recovering from its `backup_label` to consistency, then queried and stopped. `usable_s` is `total_s` plus the start's time to consistency. Since `full` is started too, an output that could not be stopped now blocks `full` as well.
- **`full_wb`'s kernel setting** is written on dest before the cache drop and reset in `run_method`'s `finally`, by writing `vm.dirty_background_ratio` back to the default recorded at the start (Linux refuses 0 for the bytes setting). Before every run of a T2 invocation a guard requires the ratio at its default and the bytes setting at 0. Settings found otherwise (full_wb's reset failed, say on a transient `sudo -n` error) are reset once by the guard, the attempt in the record's `sysctl.guard_reset`, and the run goes on if that took them back. If it didn't, the run fails before its cache drop and the trial and the invocation stop (`run.json`'s `stopped`), since every later run would fail too. Both values in force are in each record's `sysctl` (read back after full_wb's set, since `agent.py sysctl set` reports only the setting it wrote) and the CSV.
- **`probackup*` and `parcopy*`** are dispatched to `run_probackup` and `run_parcopy`. Their command lines (`probackup_argv`, `probackup_sync_argv`, `parcopy_receive_argv`, `parcopy_send_argv`) are in `run.json`'s `method_flags`. `parcopy*` stops and restarts the source as `pgdelta_direct*` does and is verified by `verify_direct`; `probackup*` is verified by `verify_probackup`.
- **`parcopy*`** (`experiments/network/parcopy.py`, the bound; standard library and `pgdelta` only) runs in `pgdelta_direct*`'s frame: one phase, `receiver_transfer`, whose steps are `receiver_start` (`parcopy receive` on dest, until it prints `listening on`) and `transfer` (`parcopy send` on the source over the harness's SSH, until both ends have exited).
  - The sender calls `pgdelta.datadir.select()` on the stopped source, so it copies v1's selection and refuses a running cluster; an unlogged relation's main fork gets its init fork's bytes. It cuts every file into ranges of at most 64 MiB, gives them largest first each to the stream with the fewest bytes (`parcopy.plan`), and sends each with `os.sendfile`, 8 or 4 TCP streams in clear. A control connection carries the inventory, then the receiver's verdict.
  - The receiver recreates every directory of the inventory with its recorded mode, the empty ones included, and creates every file, before the first byte; the top directory gets the data directory's mode. It writes with `os.pwrite`, starts write-back every 8 MiB of each range (`pgdelta.flush.start_writeback`), checks that every file got all its bytes, then fsyncs every file in parallel (16 threads), every directory bottom-up and the top directory last: the inventory's files plus its directories plus one. Not the output's parent (EXP-11's directory rule).
  - Its receiver's `--stats-json` (dest) gives the `parcopy_*` columns: per stream its bytes, first and last byte and time in `pwrite`; the fsync tail, the fsync calls and the count the inventory expects. The sender's (on the source) is kept beside it.
  - `check_parcopy` refuses a run whose VMs' parcopy lacks an option the harness passes, whose source's Python has no `os.sendfile`, or whose dest can't start write-back.
- **Durability, checked in every run** (`measure.durability_check`, the record's `durability`):
  - (a) dest's `Dirty` + `Writeback` (`/proc/meminfo`), read just before the clock starts and first after it stops, may grow by at most 4 MiB or the idle noise, whichever is larger. `--idle-noise-windows N` measures the noise at the start, as the largest increase over N idle windows of `--idle-seconds` (`run.json` `dirty_idle_noise`); `--dirty-noise-mib` passes the smoke run's, as EXP-11's run lines do (`NOISE`). Without either, a sweep judges (a) against 4 MiB alone; `analysis/exp11.py --idle-noise-bytes` can judge it again afterwards against the registered limit.
  - (b) for a full copy, dest's md array (`/proc/diskstats`) must write at least 0.98 × the output's bytes in the window.
  - A5: a `parcopy*` run fails (a) if its receiver's fsync calls differ from its inventory's files plus directories plus one, or from that count taken from its output (`verify_direct`'s `fsync_expected_from_output`); the record's `durability.fsync_count` says which, and `analysis/exp11.py` reads the count apart from the `Dirty` + `Writeback` increase.
  - A run's status stays its verification's; `durability_ok` is a column of its own, so `full_nosync` keeps its times.

**A T2 trial** (EXP-11's order within a trial):
1. the cluster and dataset, B0, the workload and its `CHECKPOINT`, as before; then `pgbench_accounts`'s file, from the catalog;
2. the start reference digest: a clean stop, `agent.py datadir-digest`, a restart. Comparisons leave out `global/pg_control`, `pg_wal/` and `pg_stat/`, which every clean stop rewrites (`measure.reference_view`);
3. the probes, untimed, each one's error recorded without stopping the trial: the durable write floor on dest (`agent.py durable-write-test`: 4 writers, as many bytes as B0 holds, write-back every 8 MiB, then an fsync of each); the cold read floor (`read-test` of the data directory without `pg_wal`, 4 readers split as EXP-07's C4) and one reader of `pgbench_accounts`'s file, on the source; the backup-mode bracket (`source_cluster.py backup-mode-probe --repeats 10`, the cache dropped before each pair);
4. the main block;
5. the extension (`--extension-repeats`, `--extension-methods`), then the warm-source block (`--warm-source-repeats`, `--warm-source-methods`), both unthrottled, each repeat in its own random order, their repeats numbered on from the main block's. In the warm-source block only dest's cache is dropped; the source runs `sync` and reads its data directory without `pg_wal` once instead (a stopped source after its stop);
6. the durable write floor again;
7. the end reference digest (`trial['reference_match']`, and `reference_differences` when the source changed);
8. the source's logical digest (the pending checks); 9. EXP-01's row-level ground truth; 10. B0's full digest.

At each speed's first visit in a trial, `set_speed` also runs an 8-stream link test from source to dest (`agent.py blast-send/blast-recv --streams 8`); every record at that speed carries its rate (`measured_s2d_gbit_8streams`), which `link_time_s` and `above_link_s` use for the multi-stream arms. Like the other probes, a test that fails is recorded (the link's `multi_stream.error`) with its rates empty, so those records have no 8-stream link time, and the trial goes on; the receiver closes a stream that fails and reports no rate from the others.

**Preflight.** A T2 run also refuses to start unless `/data` on both VMs is ext4 in `data=ordered` mode with write barriers, as `startup.sh` makes it (`env`'s `data_mount`, from `findmnt`; `measure.data_mount_problem`; the lines are in `run.json`'s `t2_preflight.data_mount`), since EXP-11's directory rule rests on the ordered journal and a dest left on XFS by `reformat_dest_xfs.sh` would break it; and without `strace` on dest (for the syscall audit), without `pg_basebackup --sync-method` where `full_syncfs` runs, with dest's `vm.dirty_background_bytes` not 0, or when the `pg_probackup` or `parcopy` arms' own checks (`check_probackup`, `check_parcopy`) fail. `remote.py provision` runs `install_incumbents.sh` after `startup.sh` on both VMs, so `pg_probackup` is on the source too, as its remote mode needs; `startup.sh` installs `strace`.

**New CSV columns:** `tier`, `block`, `source_cache`, `durable`, `durability_ok`, `dirty_increase_mb`, `disk_write_dest_bytes`, `disk_read_dest_bytes`; `pbb_checkpoint_s`, `pbb_stream_s`, `pbb_wal_end_s`, `pbb_sync_s`, `pbb_manifest_s`, `backup_mode_adder_s`; `probackup_sync_s`, `probackup_copy_s`, `probackup_stop_s`, `probackup_files_sync_s`; `parcopy_streams`, `parcopy_max_stream_bytes`, `parcopy_balance`, `parcopy_listening_s`, `parcopy_first_byte_s`, `parcopy_last_byte_s`, `parcopy_write_s`, `parcopy_fsync_s`, `parcopy_fsync_calls`, `parcopy_fsync_expected`; `usable_s`, `void`, `bytes_identical`, `measured_s2d_gbit_8streams`, `link_time_s`, `above_link_s`, `sysctl_dirty_background_bytes`, `sysctl_dirty_background_ratio`; `failure_kind` (an error's: `tool` or `harness`, for every method).

**The pg_probackup arms** (`probackup8`, `probackup4`; `run_probackup`, `verify_probackup`, `check_probackup`):
- **The command line** is `measure.probackup_catchup_argv`: `pg_probackup-17 catchup -b FULL --stream --temp-slot -j N`, the source's data directory over SSH (`--remote-proto=ssh --remote-host --remote-user --remote-path`, the directory of `--pg-probackup`, default `/usr/local/bin/pg_probackup-17`), `--ssh-options=-F <work>/probackup/ssh_config` and `--log-level-console=INFO`. Every option was checked against 2.5.16's `catchup --help` (`src/help.c`). The SSH configuration (`setup_probackup_ssh`, 0600) is the harness's key with `IdentitiesOnly`, `BatchMode`, no connection sharing and `Ciphers aes128-gcm@openssh.com`; pg_probackup adds its own `-o` options to it (`src/utils/remote.c`).
- **One timed phase, `catchup_sync`,** with two steps: `catchup`, its console lines timestamped as they arrive with `LC_MESSAGES=C`, then at once `sync -f` of the output (`probackup_sync_s`). catchup 2.5.16 fsyncs only the source's file list and `pg_control` (`src/catchup.c`); the streamed WAL, `backup_label` and every directory are left to the `sync -f`. A catchup that fails gets no `sync -f`.
- **Its phases** (`measure.probackup_phases`, the CSV's `probackup_copy_s`, `probackup_stop_s`, `probackup_files_sync_s`) come from 2.5.16's own lines: "Start transferring data files", then "wait for pg_stop_backup()" (catchup prints nothing when its copy threads end), "Syncing copied files to disk" and "Files are synced". The smoke run checks the wording (EXP-11's item 1).
- **Verification, untimed:** the output's bytes (check (b)) and WAL, counted first; the **byte diagnostic**, every file the trial's start reference digest selects under `base/` and `global/` (without `global/pg_control`) against the output's by SHA-256, the output's extra files listed (`bytes_identical`; it decides nothing; `--probackup-byte-diagnostic first_repeat` keeps it to each trial's first repeat, EXP-11's first cut); then the start, as for `full`: recovery from its `backup_label` with the WAL it streamed, no archive, no standby, no network (the server log's restored WAL files are counted, and must be 0). Its per-table digest is taken then, and the run is `pending` until the trial's step 8. An output that doesn't start fails at once.
- **At the trial's end** (`finish_logical_checks`, after the end reference digest): a digest that matches the source's passes. One that differs is **void** when the trial's two reference digests differ (the source changed; status `void`, the CSV's `void`, excluded from the baselines by the analysis) and **fails** otherwise. A void run is not a failure, and a trial with one still passes. When the source changed, a probackup* output's bytes are compared with the end reference too, and a v1 or `parcopy` run's own digest of the stopped source (`verification.reference`, against the start reference at the run) with the end one; both are reported only. The trial's `logical_check.a4` lists the runs that fail A4 (a `probackup*` whose copy didn't start or whose digest differs from an unchanged source; a `parcopy*` that failed `verify_direct` or didn't start; and a copy of either that the tool itself aborted), the arms they exclude from every baseline, and beside them the runs that ended in an error of the harness. A run ends in an error with `failure_kind` `tool` (the record and the CSV) when a command of its own timed phase exited non-zero or timed out: parcopy's receiver or sender (a dropped range makes the receiver exit 3), pg_probackup's catchup or its `sync -f`. Such an output can be neither verified nor started, so A4 counts it. Any other error (SSH, the guard, a stop or restart of the source, a stuck output) is `harness`, and fails nothing.
- **The preflight** (`check_probackup`): pg_probackup found by both VMs' `env`, the same release on both (its remote mode runs the source's binary as its agent); `catchup --help` on both lists `-b FULL` and every option passed; and dest reaches the source's binary through the SSH configuration, as catchup will (`ssh ... <remote-path>/pg_probackup-17 --version`). The result is `run.json`'s `t2_preflight.probackup`; `as_read` says whether the release is 2.5.16, the one EXP-11's reading of its flush is about.

**The syscall audit** (`--syscall-audit`, `experiments/network/durability_audit.py`): every command of every timed phase on dest runs under `strace -f -tt -y -e trace=%file,%desc -o <log>.strace`, inside GNU time, and each record's `audit` holds the check of its traces, read in order, against EXP-11's definition, for the output (and pgdelta's receipt beside it) on `--audit-filesystem` (default `/data`):
- every file written there must be covered by a later successful `fsync`, `fdatasync` or `syncfs`; `sync_file_range` counts for nothing alone;
- every directory change there (an entry made, renamed, linked or removed; an open with `O_CREAT` counts) must be covered by a later `syncfs` of the filesystem, a later `fsync` of the directory, or a later `fsync` of a file the method wrote at or after the change;
- -y names each descriptor; the open, close and dup calls keep a table for any line without it; a write to a descriptor nothing names fails the audit. Files written outside the output are listed apart and decide nothing.

`probackup*` also get `tool_only`, the catchup's trace alone: what its own flush leaves to the `sync -f`. The audit decides no status, and an audited run times nothing. `durability_audit.py summary <run directory>...` tabulates the runs' audits, and with `--json` gives each arm's result in the shape `analysis/exp11.py --audit` reads (`passed`, `uncovered`, `tool_uncovered`, paths relative to the output); `check` audits trace files and `run` traces one command line.

```sh
# EXP-11's smoke item 3: one untimed run of every arm at scale 5, under strace
python3 experiments/network/remote.py start exp11-audit -- --scale 5 --patterns hot --speeds unthrottled \
    --repeats 1 --manifest-checksums CRC32C --syscall-audit --methods $M,pgdelta_direct_inplace_j8
python3 experiments/network/remote.py wait exp11-audit --poll 30
python3 experiments/network/remote.py fetch exp11-audit            # -> results/exp04/exp11-audit
python3 experiments/network/durability_audit.py summary results/exp04/exp11-audit
python3 experiments/network/durability_audit.py summary --json results/exp04/exp11-audit > results/exp04/exp11-audit/audit.json
```

The run lines are in EXP-11's [Run plan](../../docs/experiments/EXP-11-full-copy-baseline.md#run-plan-and-budget).

**Reading a T2 run.** `report.py` keeps the warm-source block's runs apart, as rows `METHOD (warm source)`, since their source cache was read rather than dropped; the extension's runs count with the main block's at the unthrottled link, as EXP-11 compares them. Its phase table adds T2's own timers and `above_link_s`, and it lists the runs that failed the durability checks, with `full_nosync`, the negative control, counted apart. EXP-11's predictions and rules are judged by `analysis/exp11.py`, after `remote.py fetch exp11-local` and `remote.py fetch exp11-uniform`:

```sh
.venv/bin/python analysis/exp11.py --audit results/exp04/exp11-audit   # or its audit.json
.venv/bin/python analysis/exp11.py --sweeps results/exp04/exp11-smoke --tag=-smoke   # the smoke run (item 11)
```

It reads both sweeps' `summary.csv`, run records, trial records and `run.json`, and EXP-09's curated CSVs for the drift gates and v0's crossovers. It chooses each envelope's arm on the main block, pooled over the workloads; judges the HA verdict, its ratios and A3 on every run outside the warm-source block; and every other prediction on the main block. A run counts only if it passed its verification and the durability checks that apply (check (a) not once A6 voids it, check (b) for full copies, parcopy's fsync count, which stands even then), and isn't void; arms that fail A4 (a copy the tool itself aborted included, `failure_kind` `tool`, found from the record's phases for records written before the column) or that A5 reclassifies leave every baseline. An errored `parcopy*` or `probackup*` run whose kind can't be told (a summary without its records) leaves A4 undecided for its arm, which the A4 line and the verdict say. It writes `docs/experiments/data/exp11-runs.csv` and `exp11-cells.csv` and the figures `exp11-baselines.svg` and `exp11-phases.svg`; the phases figure places parcopy's last byte after its receiver's `parcopy_listening_s`, inside the transfer step. `--audit` takes the syscall audit's run directory, or the JSON `durability_audit.py summary --json` prints: each arm mapped to `{"passed": bool, "uncovered": [paths], "tool_uncovered": [paths its own flush left]}`. `--idle-noise-bytes B` (the smoke run's `run.json` `dirty_idle_noise.max_increase_bytes`) judges every run's check (a) again against max(4 MiB, B), the registered limit, whatever limit its sweep used. Other options: `--sweeps DIR...`, `--exp09-cells`, `--exp09-runs`, `--datadir`, `--figdir`, `--tag`.

## Methods for T10a

[EXP-12](../../docs/experiments/EXP-12-source-slowdown.md) asks how much each refresh slows a busy source, and whether every output taken from it is correct (G4). `--load bank` makes a run EXP-12's: its trial order, a fixed-rate pgbench load on the source, each run's settle, window and quiet tail, and the arms below. It needs `--manifest-checksums CRC32C` (B0, `full` and the staged capture), as T2 does.

| Method | Tier (`measure.TIERS`) | What runs | Source | Timed phase |
|---|---|---|---|---|
| `none` | `no_backup` | nothing: `--none-window-s` (60 s) with the counter reads every arm makes | running, loaded | **none** |
| `full` | `omni_today` | EXP-11's `full` | running, loaded, cold | **transfer** |
| `capture` | `staged_capture` | the staged capture alone: a local `pg_basebackup` of S on the source (CRC32C, `-c fast`, `--verbose`), its lines stamped as they arrive over SSH | running, loaded, cold | **capture** |
| `pgdelta` | `staged_v0` | EXP-04b's arm; its capture now `--verbose` and stamped over SSH | running, loaded, cold; then S | **receiver_capture**, then **transfer** |
| `bracket` | `backup_mode` | `pg_backup_start('t10a', true)` then `pg_backup_stop(false)`, one psql session | running, loaded | **bracket** |
| `v1proxy` | `v1_proxy` | backup mode held on the live source (`source_cluster.py backup-hold`) around `pgdelta_direct_inplace`'s engine reading the snapshot | running, loaded; the snapshot read cold | **backup_transfer**: steps **backup_start**, **receiver_start**, **transfer**, **backup_stop** |
| `v1proxy_s1` | `v1_proxy_one_core` | as `v1proxy`, the sender at `--jobs 1`, the receiver at 4 | as `v1proxy` | as `v1proxy` |
| `pause2` (control) | `positive_control` | `agent.py load-pause 2.0`: SIGSTOP, 2 s later SIGCONT, to the load's pgbench, both stamped on the source's clock | running, loaded | **control** |
| `burn4` (control) | `cpu_reference` | `agent.py burn --procs 4 --seconds 2.0` | running, loaded | **control** |

The proxies are in `PGDELTA_DIRECT_METHODS` (so every dispatch takes v1's path) but not in `SOURCE_STOP_METHODS`: the live source is never stopped for them. Their `source_data` (`method_paths`, `pgdelta_send_argv`) is the snapshot, and `verify_direct` checks B against the snapshot's selection as its digest was stored at the trial's step 5, never reading the source. Check (b) covers `pgdelta`'s new O (`measure.DISK_WRITE_CHECK_METHODS`); `FULL_COPY_METHODS` is unchanged.

**`run_exp04.py`'s options for T10a** (the run line's; each defaults to the value EXP-12 registers):

| Option | Default | What |
|---|---|---|
| `--load bank` | none | EXP-12's load and trial order |
| `--load-rate R`, `--moderate-rate R` | 0 | the busy and warm blocks' `-R`, and the moderate block's; 0 parses (the run line's shape) and `execute` refuses it |
| `--load-clients`, `--load-threads`, `--load-warmup-s`, `--load-duration-s`, `--load-seed`, `--load-log-dir`, `--load-signal` | 32, 4, 120, 14400, `--order-seed`, `/dev/shm/exp12`, ALRM | pgbench's `-c`, `-j`, the warm-up after each start, `-T`, `--random-seed`'s base, its tmpfs, how a run is stopped |
| `--capacity-warmup-s`, `--capacity-s`, `--capacity-runs`, `--capacity-rest-s` | 30, 60, 1, 30 | step 7's C (the smoke run takes 3 runs) |
| `--settle-s`, `--settle-max-s`, `--tail-s`, `--window-tail-s`, `--none-window-s` | 15, 60, 15, 10, 60 | the run's timeline |
| `--source-cache evict\|keep`, `--snapshot`, `--static-digest every\|first_repeat\|none` | evict, off, every | the eviction, the proxies' snapshot, (vi)'s logical digest |
| `--controls pause2,burn4` | none | the controls at the busy block's start and end, and `pause2` at the moderate block's start |
| `--warm-source-repeats`, `--warm-source-speeds`, `--warm-source-methods` | 0, unthrottled, none | the warm block (EXP-11's options, with speeds) |
| `--moderate-repeats`, `--moderate-speeds`, `--moderate-methods`, `--moderate-method-speeds` | 0, none | the moderate block |
| `--calibration-repeats`, `--calibration-speed`, `--calibration-methods`, `--calibration-idle-s` | 0, 2, none, 30 | the calibration block, without load (any method or control, v1 itself among them) |
| `--load-check-wait-s`, `--idle-rate-s`, `--sampler-interval-s`, `--clock-samples` | 20, 20, 0.1, 50 | step 15's wait, step 6's idle window, the sampler's tick (0: no sampler), the clock probe |
| `--oracle-controls` | off | smoke item 5: the oracle's seven negative controls, under the load |

**New commands.**
- `source_cluster.py`: `loaddb-setup`, `load-start`, `load-stop`, `load-capacity`, `load-status`, `load-check`, `backup-hold`, `snapshot` (its docstring). The load's registry, `load.json`, sits with its logs in `--load-log-dir`.
- `agent.py`: `evict`, `fincore`, `burn`, `load-pause`, `settle-gate`, `clock-probe`, `sampler start|stop|run`, `compress`, and `run --tee`; `env` adds `chrony`, `zstd`, `fincore` and `/dev/shm`, and pgbench's, pg_amcheck's, pg_dump's and pg_test_fsync's versions. `startup.sh` installs `zstd`, and `util-linux-extra` where `fincore` is missing.

**The load.** loaddb, beside the benchmark's database: `pgbench -i -s 2 -F 90`, `load_progress` (one row per client, each on its own page), `CREATE EXTENSION amcheck`, autovacuum off on its tables, and `measure.LOAD_SERVER_CONFIG` appended to the source's configuration, then a restart. Two scripts (`experiments/network/load/`): `commit.sql@99` adds exactly 1 to the sum of `abalance` and 1 to the client's `n`; `abort.sql@1` rolls back a change of 10^9. Every pgbench run (the capacity runs, the busy and moderate loads, a restart) records each client's `n` and both sums with the load stopped, before it starts and after it stops; `load-stop` sends SIGALRM, the signal pgbench's own `-T` uses, then counts each client's commits in its logs: `complete` when they equal the change in `n`. `load-check` (step 15) wants the sums equal to every logged run's commits, every run complete, and each run starting where the last stopped.

**Each run** (`run_load_method`; `run.json`'s `t10a.timeline`):
1. the source, untimed: the previous runs' source work (`pending_source`: S checked with `pg_verifybackup` and, for `pgdelta`, its SHA-256 digest against O's taken on dest; pgdelta's statistics read; S removed), the load checked alive (if it died, the run before is void and the load restarts; the run's `load_run` is then the restart's, read after this step), and the eviction (`POSIX_FADV_DONTNEED` of `base/<postgres>` and the snapshot; once more after `sync -f` if more than 1% stays) or, in the warm block, one read of them; then `fincore` of them and of loaddb;
2. the settle: dest's set-up (B for the proxies and v1) and cache drop, uncapped (`dest_wait_s`), then `agent.py settle-gate` on the source until its last 10 progress lines are within 5% of R with a mean lag under 1 ms, after at least `--settle-s` and at most `--settle-max-s` (else `unsettled`);
3. the timed phase or phases, dest's `Dirty + Writeback` and md writes read just before the clock starts and first after it stops; `window` records t0 and t1 in UTC on dest's clock, and on the source's through the clock probe's offset;
4. the quiet tail, `--tail-s` from the clock's stop: dest's verification inside it, touching nothing on the source, then the rest waited out, and then until the analysis's 15-s-tail view has ended too (its last 1-s bucket ends at t0 + ceil(t1 + 15 - t0), up to 1 s past t1 + 15; `tail.view_end_utc`, 50 ms past it), so no view holds the next step on the source. A verification longer than the tail runs on to its end (`tail.overran_s`), so the next run's source step, and its window, wait for dest's work, as EXP-12's settle asks.
The calibration block has no load, gate or tail; v1 itself stops and restarts the source there, and only there. A planned stop of the load (the switch to the moderate rate, step 15, the clean-up) that finds pgbench already gone (`load-stop`'s `how` `ended`) voids the latest run on that load run too, which no later check would have looked at (`load.died_before_stop`).

**The oracle** (`verify_live`, for `full` and `pgdelta`): `pg_verifybackup` (O with S's manifest), then the copy started on port 5433 recovering from its `backup_label` with only its own WAL, and while it runs (`oracle_checks`): (iii) the invariant on loaddb (`measure.load_oracle_sql`, with each client's `n`), (vi) the benchmark database's row counts or its logical digest, `pg_amcheck --heapallindexed --verbose` of loaddb (a skipped database or no relation checked is unverified), and (vii) `pg_dump --schema-only --restrict-key=exp12` of loaddb and postgres, hashed. An error from the copy's server or tools is wrong; only a timeout, a missing binary or an SSH failure (S's check on the source) is unverified. A copy its own tool aborted (a command of its timed phase exited non-zero or timed out: `pg_basebackup`, pgdelta's receiver or sender, the proxy's `backup-hold`) is wrong too, status `fail` with `failure_kind` `tool`, for every arm with an output (`measure.TOOL_ABORT_WRONG_METHODS`: `full`, `pgdelta`, `capture`, the proxies, v1 itself): it doesn't exist, so it can't start, and R3 and L5 read it (A4 also excludes a proxy so aborted). Its quiet tail is still waited out. A failure of the harness around it (`ssh` exiting 255 under `backup-hold`, or a command stamped over SSH whose agent wrote no result line) is an error of kind `harness`, which the analysis reports as unverified; a tool's failure on an arm with no output (`bracket`, the controls) stays an error of kind `tool`. (iv) the consistent cut and (v) coverage wait for the logs: at the trial's end, `finish_load_checks` maps each copy's `n` to the logged run that holds it, reads the commits on either side of its cut, and judges both, with G4's staleness. A live output's status is `measure.oracle_status` of its checks: `fail` on any wrong, `pending` while one waits, `unverified`, or `pass`.

**Reading an EXP-12 run.** Each record has `block`, `block_repeat`, `control_slot`, `load_rate`, `load_run`, `void`, `clock_offset_s`, `source_untimed` (with `cache.fincore` and `cache.cache_flag`), `settle` (with `gate`, `unsettled`, `dest_wait_s` and the source's `busy_rate_no_steal`), `window`, `tail`, `durability`, and for live outputs `verification.oracle` and `g4`; the proxies' `proxy.backup` (A4 (ii)) and `proxy.hashed` (A4 (i)). The trial holds `load` (every run with its counters, `capacity`, `warmups` with their null pool's last 60 s, `restarts`, `end` with `check`, the sampler and the fetched logs), `references`, `snapshot`, `probes`, `clock`, `controls`, `oracle_controls` and `load_checks`. The logs are fetched, zstd-compressed, to `<out>/logs/<trial>/load/`, with the sampler's `sampler/sampler.jsonl` (0.1-s ticks) and `sampler/sampler-sql.jsonl` (1-s SQL). Whatever ends a trial early (an error, or `remote.py stop`'s SIGTERM), its clean-up (`end_load_quietly`) keeps them: the load and the sampler stopped, the logs compressed (best effort; if that fails, they go as they are), fetched, and read for the cut and coverage of the outputs taken so far (`load.kept_after_error`, `load_checks`). The load's directory is removed from the source only once its logs are on dest; otherwise it is left there and named in `cleanup_errors`. The CSV's columns only the logs give (W, D, latencies, the client's wait, most flags) are empty until `analysis/exp12.py` fills each record's `slowdown`.

**Running T10a.** The run lines are in EXP-12's [Run plan](../../docs/experiments/EXP-12-source-slowdown.md#run-plan-and-budget). As written there, `R_BUSY=0` and `R_MOD=0` parse, and `execute` refuses them before it touches either VM; the smoke run sets them (its item 2), `NOISE` (item 13) and `L_MS` (item 3). `tests/test_exp12_integration.py` parses that block exactly as written. The smoke run's items, and what the harness gives each:

| Item | From the harness |
|---|---|
| 1. pgbench's log | every run's `load-stop` (`complete`: each client's commits in its log against the change in its `n`) and step 15's `load-check`; `--load-signal INT` is the fallback. pgbench 17 doesn't catch SIGINT, so its threads' buffered last lines are lost (on PostgreSQL 17 here, about 30 per client) and the counters can't match: a run stopped so is judged by the fallback's registered rule instead (`completeness.criterion` `sigint`: each client's transaction numbers contiguous from 1, its last line at or after the signal less 1 s), and `load-check` sums its counters (`committed`) |
| 2. Capacity and rates | `--capacity-warmup-s 60 --capacity-runs 3`: C is the median of the three (`trials/*.json` `load.capacity`). Since `execute` refuses a rate of 0, C comes from a first invocation at a provisional `--load-rate` any pair sustains (1000, say); the busy CPU at each rate is each settle's `busy_rate_no_steal`. `--load-threads 8` is the fallback |
| 3. L and the null | an invocation of `--methods none --repeats 5` at R_busy: its five `none` runs and every settle, and the busy warm-up's p99 (the analysis prints both) |
| 4. Clocks | the clock probe at the trial's start and end (`trials/*.json` `clock`), and `env`'s `chrony` on both VMs. Each probe is tried up to 3 times, 2 then 4 s apart (`failed_attempts` keeps the errors); without an offset at the start, the trial ends in an error before the load starts, since no window could be mapped |
| 5. The oracle | `--oracle-controls`: after the trial's capacity runs and under its load, the seven negative controls and a good copy; every `full` and `pgdelta` output besides. Both VMs' versions are `run.json`'s `t10a_preflight.versions`, which refuses two minor versions; the schema hashes are the references' |
| 6, 7. The proxies | `v1proxy` and `v1proxy_s1` in a busy pass: `proxy.backup`, `proxy.hashed`, and the sender's `--jobs` in `tool_reported.switches` |
| 8. Eviction | each cold run's `source_untimed.cache`; the second eviction after `sync -f` is automatic |
| 9. The capture's lines | `g4.capture_end` (`pgdelta`) and `tool_reported.basebackup_phases` (`capture`) |
| 10. The settle gate | each loaded run's `settle.gate` and `settle.seconds`; the minimum is `--settle-s` |
| 11. The sampler's cost | the sampler's own CPU in `load.end.sampler`; the p99 without it from an invocation with `--sampler-interval-s 0`, its fallbacks `0.5` and `1`. The analysis compares the busy warm-up's p99 over its last minute with and without it (that invocation's sweep given to `--sampler-off`) and prints the verdict |
| 12. `pause2` | `--controls pause2,burn4`: `pause2` at the busy block's start and end |
| 13. Dest's checks | `--idle-noise-windows 20` (`run.json` `dirty_idle_noise`, the run line's `NOISE`); `full_nosync` in `--methods`, at 2 Gbit/s by `--method-speeds`; the audit, `--syscall-audit --scale 5 --methods full,pgdelta,v1proxy` (with the load and `--snapshot`), in an invocation of its own |
| 14. Stability | `references.start` and `references.end` (loaddb's pages and growth, the logical digest, the schema), the sampler's checkpoints and autovacuum workers |
| 15. The projection | a busy pass (`--repeats 1`) and `--calibration-repeats 1`; the analysis projects the registered runs, a warm or moderate kind that didn't run at its arm's busy median, adds the budget's fixed parts (20 min to provision, the smoke's 60, the trial's set-up 8 and end 4.5, 15 to fetch and tear down) and 28 speed visits of 8 s, and applies the cuts in their order until the pair's time is within 3.5 h |
| 16. The analysis | `analysis/exp12.py --tag=-smoke`, into a scratch directory (`--datadir`, `--figdir`), as below |

EXP-12's predictions and rules are judged by `analysis/exp12.py`, after `remote.py fetch exp12-hot`:

```sh
L_MS=10  # smoke item 3's L
C=0; BUSY_CPU=0; NOISE_BYTES=0  # smoke items 2 and 13: C, the busy CPU at R_busy (a share), check (a)'s idle noise
.venv/bin/python analysis/exp12.py --sweeps results/exp04/exp12-hot --late-limit-ms $L_MS \
    --null-resamples 10000 --null-seed 121 --client-wait-share 0.25 \
    --capacity-tps $C --busy-cpu $BUSY_CPU --idle-noise-bytes $NOISE_BYTES
# the smoke run (item 16), into a scratch directory under results/, which git ignores; item 11's sweep without
# the sampler beside it
.venv/bin/python analysis/exp12.py --sweeps results/exp04/exp12-smoke --tag=-smoke \
    --sampler-off results/exp04/exp12-smoke-nosampler \
    --datadir results/exp04/exp12-smoke/analysis --figdir results/exp04/exp12-smoke/analysis
```

It reads each sweep's run records (in `summary.csv`'s order), trial records and `run.json`, and the load's logs: each logged pgbench run once, a million lines at a time (zstd-compressed or not), every transaction fed to the stretches of the source's clock that need it (each run's window from 10 s before t0 to 15 s after t1, its settle, every `none` span, the warm-ups' last minute). Per run it takes O4's two numbers on 1-s buckets from t0 to t1 + 10 s (W, the deficit of completions against the schedule over R; D, the worst bucket's 1 - X/S), the latencies of the transactions scheduled in [t0, t1], the worst 5-s span's p99, the late count, the backlog, and every robustness view's statistic (D in 0.5-s and 2-s buckets, the late count at L and 2L, a 15-s tail); with the sampler's data, the steal, client, client wait, checkpoint and autovacuum flags; and the settle gate's log part. The null is a block bootstrap of each block's load-only time (`none` spans, settles that passed their gate, the warm-ups' last minute; the warm block shares the busy block's), each run's window drawn at its own T from a seed of its own; each cell's median (or maximum) gets the null of the same statistic, and the net values, "measurable", the bound and δ (centred) follow EXP-12. A run counts if it passed its verification and the durability checks that apply, isn't void and its log covers its window; a cell with fewer than 3 runs (2 for a control) isn't judged. It prints, in EXP-12's order of judgment, G4 and R3, A1 (with the analysis-only coverage control), A13, A2, A3 and the clock gate, L1-L5, R1, R2, decision 1 with A4 and the warm reading, decision 2, A5, A6, the rows beside, the flags (fragile from every robustness view, near the line, the latency caveat), E1-E16, A1-A13, and the smoke run's sixteen items from the records. It writes `docs/experiments/data/exp12-runs.csv` (each record's `measure.summary_row` with its `slowdown` set, plus the views' numbers) and `exp12-cells.csv` (every cell in every view), and the figures `exp12-timeseries.svg` and `exp12-loss.svg`. Its options: `--sweeps DIR...` (default `results/exp04/exp12-hot`), `--late-limit-ms` (L, smoke item 3's; 2L is the second late-count view; default 10), `--null-resamples` (default 10,000, at least 100), `--null-seed` (default 121), `--client-wait-share` (A8's *client wait* share, default 0.25), `--capacity-tps` (the smoke run's C, A12; not judged without it), `--busy-cpu` (the busy CPU at R_busy as smoke item 2 recorded it; it selects E4's, E7's, E9's, E14's and E16's ranges; without it, the smoke run's own analysis takes its busy settles' median, which is what item 2 records, and the main sweep's selects no branch, so the sweep's own settles can't pick another than the registered one), `--idle-noise-bytes` (check (a) judged again against max(4 MiB, this)); a 0 for C or the busy CPU (the run line's placeholders) is refused, `--sampler-off DIR...` (sweeps run with `--sampler-interval-s 0`, read only for smoke item 11's p99), `--datadir`, `--figdir`, `--tag`.

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
  - **Per-phase floor.** Every timed phase has its own counter window, and each window holds the exit of its "before" snapshot agent and the start of its "after" one (python3 over SSH on the source). `run.json` `phase_floor` measures this with three empty phases at start-up, per VM. The totals don't subtract it. `counter_windows` says how many windows a total holds: 1 for `full`, `pgdelta_direct*` and `pgbackrest_*`, 2 for `incremental`, `pgdelta` (and T3's methods) and both rsync variants. (Before the overlap, rsync had a third, source-idle window for its prep.)
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

For T3, T8a and T11 (see [Methods for T3, T8a and T11](#methods-for-t3-t8a-and-t11); speeds and repeats here are examples):

```sh
python3 experiments/network/remote.py provision     # VMs made before pgBackRest and xfsprogs were installed

# T3 unthrottled and T8a at 2, 5 Gbit/s and unthrottled, in one run
python3 experiments/network/remote.py start t3-t8a -- --speeds 2,5,unthrottled --repeats 5 \
    --methods full,pgdelta,pgdelta_gapfill_seq,pgdelta_seq,pgdelta_fsync_wb,pgdelta_fsync_worker,pgdelta_inplace,pgdelta_direct,pgdelta_direct_inplace \
    --method-speeds pgdelta_gapfill_seq+pgdelta_seq+pgdelta_fsync_wb+pgdelta_fsync_worker+pgdelta_inplace=unthrottled

# T11 in its own run, so its archiving touches no other test's numbers
python3 experiments/network/remote.py start t11 -- --methods full,pgdelta,pgbackrest_zst,pgbackrest_none,pgbackrest_block

# T3 (d): XFS on dest, then the T3 methods again, with the methods that can clone
python3 experiments/network/remote.py ssh dest sudo ~/pgd/infra/gcp/reformat_dest_xfs.sh --yes
python3 experiments/network/remote.py start t3-xfs -- --speeds unthrottled --repeats 5 \
    --methods full,pgdelta,pgdelta_inplace,rsync_8k,incremental_cfr
```

`run_exp04.py --help` lists every option:
- the trial options `--patterns`, `--fractions`, `--scale`, `--seed`, `--hot-percent`, `--events-rows` and `--batch-size`;
- `--speeds` (Gbit/s values and `unthrottled`), `--methods`, `--method-speeds` (some methods at some speeds only) and `--repeats`;
- `--pgbackrest` (default `/usr/bin/pgbackrest`, on both VMs);
- `--order-seed` (method orders, and speed orders from their own seeded stream), `--speed-order` (`shuffle`, `alternate` or `ascending`), `--link-test-seconds`, `--idle-seconds` and `--disk-test-mib`;
- `--gnu-time` (default `/usr/bin/time`; `none` skips peak memory);
- `--keep-outputs`, `--keep-cluster` and `--stop-on-error`.

`--methods` defaults to `full,incremental,pgdelta,rsync_8k,rsync_default`. `pgdelta` runs EXP-06's engine and `pgdelta_1thread` EXP-02's, each pinned by its flags (`measure.PGDELTA_ENGINES`), never pgdelta's defaults. Add `pgdelta_1thread` (for example `--methods full,incremental,pgdelta,pgdelta_1thread,rsync_8k,rsync_default`) to run both, as EXP-04's design asks once parallel hashing is merged. The run checks `pgdelta send --help` and `receive --help` on both VMs first for every flag the selected methods pass (the default methods: `send --jobs`, and `receive --jobs --rebuild --verify --prefetch-basis`), and refuses to start without them. Each pgdelta record's `tool_reported` holds the flags passed, the `switches` pgdelta reported as in effect, and `switches_ok`, whether they include the pinned engine's. `run.json` `method_flags` holds every selected method's pinned flags. The optional methods of T3, T8a and T11 are described in [their own section](#methods-for-t3-t8a-and-t11).

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

The T3, T8a and T11 methods add:
- **in `run.json`:**
  - `method_flags`, every selected method's pinned flags;
  - `method_speeds` and `methods_by_speed`;
  - `pgdelta_required_options`, which `pgdelta_jobs_check` checked;
  - `pgbackrest`: null, or the configurations, SSH set-up and archive command of a pgBackRest run;
  - per VM in `environment`: `pgbackrest` (its version) and `data_reflink`.
- **in `trials/<trial>.json`:** `pgbackrest` (the set-up's results and the backups' sizes) and `logical_check` (the source's digest and each pgBackRest run's match).
- **in each run record:** `preparation` (the untimed set-up's commands, and for a copy of B0 its `shared_extents`), `source_restart` (pgdelta_direct\*), and in `verification`: `in_place_leftovers`, `output_start`, `logical`, `logical_match`, and for every method `shared_extents`.
- **in the logs on dest:** `<method>-setup-copy.log` and `-setup-sync.log`, `<method>-restore.log` and `-pgbackrest-restore-file.log`, and `<method>-output-server.log`, `-output-start.log`, `-output-recovery.log`, `-output-query.log` and `-output-stop-*.log`.
- **in the logs on the source:** `pgbackrest-<stanza>-*.log`, and `cluster/start-*/server.log` after each restart.

`report.py summary.csv` prints, per trial:
- the median `total_s` of each method at each speed, the ratio to the full copy, and the first speed at which each delta method is no longer faster. This is the headline, and its crossover is computed on `total_s`.
- `rsync_* (serial)` and `pgdelta* (serial)` rows: the same for `serial_total_s`, the upper bound with the concurrent phase's two parts (rsync's copy of B0, or pgdelta's receiver start-up, and the capture) one after the other.
- `rsync_* (in place)` rows: the same for `inplace_total_s`, the lower bound without the copy of B0.
- a second, labelled view of the same tables on `total_no_ssh_s`, with the remote-launch overhead taken out.
- a table of median timed phases per method and speed: `receiver_start_s`, `capture_s`, `prep_s`, `transfer_s`, `combine_s`, `restore_s`, then pgdelta's own `rebuild_s`, `fsync_s`, `fsync_wait_s` and `verify_wait_s` (inside its transfer), and `total_s`, with only the columns some run has. Runs still `pending` are listed with the others that did not pass.

The new methods appear in the same tables, with their ratios to the full copy when `full` ran in the same invocation. T3's staged methods also get `(serial)` rows.

CSV columns:
- `run_name`, `trial_id`, `pattern`, `fraction`, `scale`, `rows_changed`, `repeat`;
- `speed`, `speed_index` (its position in the repeat's speed order), `target_gbit`, and `measured_s2d_gbit` / `measured_d2s_gbit` from the link test;
- `method`, `order_index`, `status`;
- `total_s` (end to end), `total_no_ssh_s`; rsync only: `inplace_total_s`, `inplace_total_no_ssh_s`; rsync and pgdelta: `serial_total_s`, `serial_total_no_ssh_s`;
- `prep_s` (rsync only), `capture_s`, `prep_capture_s` (rsync only), `receiver_start_s` (pgdelta: a part of `receiver_capture`, or the first step of pgdelta_direct\*'s `receiver_transfer`) and `receiver_capture_s` (staged pgdelta only), `transfer_s`, `combine_s`, `restore_s` (pgBackRest only), `ssh_overhead_s`;
- `untimed_setup_s`: the untimed set-up before the cache drop (the rsync daemon's start; the copy of B0 to B; the clean stop of the source);
- pgdelta only, from its receiver's statistics: `prefetch_bytes` (B0 read into the page cache while it waited), `prefetch_basis_bytes` (B0's size), `prefetch_s` (time spent reading), `prefetch_wait_s` (from the receiver listening until the sender connected) and `prefetch_stopped` (`finished`, `cap`, `error`, or `connected` if the sender came first);
- pgdelta only: `untimed_gap_s` (between `receiver_capture` and the transfer, untimed; empty for pgdelta_direct\*, which has no gap), `prefetch_untimed_s` (how long the prefetch read B0 in that gap, off the clock) and `prefetch_untimed_bytes_est` (the bytes, at the prefetch's average rate); `hash_basis_s` (the receiver hashing B0 for the sender, inside the transfer);
- pgdelta only, its own timers inside the transfer: `rebuild_s` (the write phase), `fsync_s` (of which flushing O's files), `fsync_wait_s` (the main loop waiting for flushes done elsewhere, when pgdelta reports it) and `verify_wait_s`; and the modes it reported: `in_place`, `fsync_mode`, `source_kind` (empty before pgdelta reports them);
- `wire_s2d_bytes`, `wire_d2s_bytes` and the receive-side cross-checks (pgBackRest: the restore plus its restored copy's recovery);
- pgBackRest only: `recovery_wire_s2d_bytes`, `recovery_wire_d2s_bytes` (of those, what the recovery's `archive-get` used, untimed) and `recovery_wal_files` (the WAL files it fetched);
- `tool_s2d_bytes`, `tool_d2s_bytes`, `output_bytes`, `output_minus_wal_padding_bytes`, `output_shared_bytes` (the output's bytes in extents it shares with another file after the run, from FIEMAP: a clone of B0 on XFS), `capture_bytes`;
- `wal_bytes`, `wal_stream_bytes`, `wal_padding_bytes`;
- `cpu_source_s`, `cpu_dest_s` (end to end), `prep_capture_cpu_dest_s` (rsync only), `receiver_capture_cpu_dest_s` (pgdelta only), `counter_windows`;
- `proc_cpu_source_s`, `proc_cpu_dest_s`, `peak_rss_source_kb`, `peak_rss_dest_kb` and `proc_scope` (not comparable across methods; see **Per process**);
- `verified`, `identical_to_s` (staged methods: against S; pgdelta_direct\*: against the source's selected files), `logical_match` (pgBackRest: the per-table digest against the source's, empty while `pending`), `output_start_s` (pgdelta_direct\* and pgBackRest: starting the output on dest, untimed), `caches_dropped`, `error`.

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
- **The source is otherwise idle** (outside EXP-12's `--load`, whose fixed-rate load is the point). No workload runs during the backups. Autovacuum stays on, as in EXP-01, and a long sweep gives it time to run: at the default 1% fractions no table reaches its threshold, but a 10% fraction would trigger an autoanalyze. `trials/*.json` has `pg_stat_user_tables` after the workload and at the end, so a run can be checked.
- **Each capture adds a checkpoint and a WAL segment to the source.** So S differs from run to run by pg_control, the backup label and a new 16 MiB WAL segment, which is zero-padded on disk. Every method still sees the same data pages. But the staged methods (pgdelta, rsync) send that whole segment, about 16.8 MB per run, where `full` and `incremental` stream only the few KB of real WAL. That's a fixed per-run cost of the staged design, recorded per run as `wal_bytes` and `wal_padding_bytes` (see **WAL** above).
- **Plain format, default tablespace, 8 KiB blocks**, PostgreSQL 17.11 only.
- **The T3, T8a and T11 methods have not run on the VMs yet.** The pgdelta options they pin (`--fsync`, `--in-place`, `--source-kind`, `pgdelta.datadir`) are being built at the same time; until they are deployed the pre-run check refuses these methods. pgBackRest's configuration, its SSH set-up, the restore and the archive recovery have only been checked as command lines and texts, not run.
- **pgBackRest's transport differs.** Its restore goes over SSH, which encrypts, with 4 connections; pg_basebackup, pgdelta and rsync use one or two unencrypted TCP connections. Where the link is fast, SSH's CPU cost may limit it.
- **Archiving runs in the background in a pgBackRest run.** Every staged capture ends a WAL segment that the source pushes to every stanza, during whatever runs next; that CPU is in `cpu_source_s`. Hence T11 in its own run.
- **pgBackRest runs pass or fail only at the end of their trial,** when the source's digest is taken. `--stop-on-error` doesn't stop on a digest mismatch, and a trial that ends early leaves its pgBackRest runs `pending`.
- **In place, the copy of B0 is not timed.** That is the point of patching in place: B already exists. B is a fresh `cp -a --reflink=never` of B0, synced and then evicted from the page cache, not a copy that has aged on disk.

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
- the T3, T8a and T11 methods:
  - every new method is optional; each new pgdelta method's send and receive flags, exactly; the pre-run check asks for each selected method's options and for `pgdelta.datadir`, and refuses without them; switches compared on what a method pins; the modes found wherever pgdelta's statistics keep them, and the fsync mode read as pgdelta's receiver reports it (`fsync_effective` before `fsync`; a writeback that fell back to main fails `modes_ok`; the timings' seconds are not a mode);
  - the pinned flags parsed by pgdelta's own command line, for the new methods only once pgdelta has their options (skipped until then);
  - `--method-speeds`: parsing, refusals, and the methods each speed runs in a trial;
  - `incremental_cfr`'s `pg_combinebackup --copy-file-range` (and `incremental`'s `--copy`, as before), its paths, logs and pinned flags;
  - the copy of B0 for the in-place methods and pgBackRest with `cp -a --reflink=never`, and a copy that FIEMAP finds sharing blocks failing the run before the cache drop; FIEMAP itself (the structures' sizes against the ioctl number, batches, the shared flag, a filesystem without FIEMAP) and the CSV's `output_shared_bytes`;
  - stubbed runs: T3's methods staged with their flags; `pgdelta_inplace`'s copy of B0 before the cache drop, its receiver with `--basis B --in-place` and no `--output`, and its untimed set-up outside `total_s`; a failed copy stops the run before the cache drop; `pgdelta_direct`'s clean stop before the cache drop, its one timed phase with two steps and no counter read between the receiver listening and the sender's start, a receiver that never listens (no transfer), its sender's `--source-kind datadir --source` on the data directory, and the restart after the verification; the restart after a failed transfer and after a failed stop; a trial that stops when the source won't restart, and the next trial, which runs all its methods;
  - pgBackRest: the restore's command line and its copy of B0 before the cache drop; the set-up only in runs that select it, before B0, and the backups after the workload; the set-up's commands on both VMs (configuration, archive command through `source_cluster.py archive`, `stanza-create` and `check` per stanza, `info` from dest); the backups and their sizes; the configuration texts, parsed, with `compress-level-network=0` in dest's `pgbackrest_none` stanza only; the restore log and `info` JSON parsed; pending runs that pass or fail against the source's digest at the end of the trial, with their record and the CSV rewritten; the counters read around the start of the restored copy, its bytes added to the wire totals and kept apart, and its restored WAL files counted from the server log;
  - verification: the output's start command line; waiting for recovery; the server stopped whatever happens; one that can't be stopped left in place, with later output starts and its path refused and the other methods running; a server an earlier run left on the port, found from its lock file; a direct output against the selected files (a changed page, an extra file, a selected file the source lacks); in-place leftovers, staged and direct; `files_digest`, `parse_datadir_list` and the agent's `datadir-digest`, with `list --sources` (an unlogged relation's main fork hashed from its init fork, as pgdelta sends it, and matching an output that holds it so) and its fallback to plain `list`; the logical digest's query and comparison;
  - `source_cluster.py`: its start and stop command lines, a stop that doesn't end `shut down`, a start that leaves a running cluster alone, the archive settings' quoting;
  - `reformat_dest_xfs.sh` refusing without `--yes`, without root, on pgd-source or another host, and without the array, with every destructive command replaced by one that fails; `startup.sh`'s packages; `remote.py provision`;
  - the new CSV and report columns, ratios and the phase table;
  - on a real PostgreSQL, when its binaries are present (`PGDELTA_TEST_PG_BIN`): an output whose configuration listens on an address the host lacks and archives starts with the harness's settings, and its per-table digest is stable and follows a changed row.

`python -m unittest discover -s tests -p test_exp11_core.py` covers T2's core without a VM: the method tables and command lines; `run.json`'s `method_flags` with each new arm's whole command line, the blocks and the preflight (an `execute()` with its VM steps stubbed); the CRC32C guard and the block options; the ext4 preflight on both VMs; `Child`'s stamped lines, a clock that stops at the command's exit while a process holding its output lives on, and the phase parsers and the adder on PostgreSQL 17's `pg_basebackup --verbose` lines (with and without a sync, and cut short) and on `pg_probackup catchup`'s; `/proc/meminfo` and `/proc/diskstats`, and checks (a) and (b) with the 4 MiB limit and the idle noise; one counter window per arm, the flush inside it (the pg_probackup block's `run_probackup` with its `sync -f`); `full_wb`'s set and reset, after an exception too, with both values checked and recorded, and the guard's own reset when one failed, or the invocation stopped when that fails too; the warm-source block; the trial's order, its blocks and a changed source; the 8-stream link test, and one that fails; `remote.py`'s record of `pgdelta/`'s diff since EXP-09's deployed commit; and the agent's and the source's probes (a 3-stream blast over loopback, a stream that fails, the durable write and read tests on temporary files, `sysctl`, `env`, the backup-mode bracket).

`python -m unittest discover -s tests -p test_parcopy.py` covers `parcopy` without a VM: the plan on sizes shaped like scale 70 (every byte in one range of at most the chunk, largest first to the lightest stream, the busiest stream within 1.1 × the mean where a split by file would not be); copies over loopback with 1 and 4 streams from a synthetic stopped data directory, byte-identical to its selection, with every file and directory fsynced once and the top directory last, counted through a patched `os.fsync` against the inventory's files plus directories plus one; the recorded modes under a stripping umask; write-back every `--writeback-bytes` of each range; a dropped range, which fails the copy and `verify_direct`, and through `run_method` ends the run in the tool's own error, which A4 counts; the refusal of a running cluster (a `postmaster.pid`, a state other than `shut down`), of which a waiting receiver is told; the command line end to end; with PostgreSQL 17's binaries (`PGDELTA_TEST_PG_BIN`), a real stopped cluster whose copy starts without recovery, and a running one refused. With the orchestrator's stubs: `run_parcopy`'s one window after a clean stop, a receiver that never listens, a failed sender; `check_parcopy`; A5's fsync count in check (a).

`python -m unittest discover -s tests -p test_exp11_probackup.py` covers the pg_probackup arms without a VM, pg_probackup or strace: the catchup command line against 2.5.16's `catchup --help`, the version parse and the SSH reach command; one counter window with the `sync -f` inside it, after the catchup, and no `sync -f` after a failed catchup; the verification (bytes counted before the start, the byte diagnostic and its cut, a pending digest, an output that doesn't start); the end of the trial (pass, fail, void, A4, the reported-only comparisons with the end reference, and a trial with a void run passing); the preflight and its refusals; `install_incumbents.sh`'s link to the path the harness runs; and the audit in the orchestrator (a `Child` under a strace stand-in inside GNU time, only the timed phase's commands traced, `tool_only`, the preflight). `python -m unittest discover -s tests -p test_durability_audit.py` covers the audit on synthetic traces: a file never fsynced, a directory covered only through a later file's fsync, a pg_probackup-like trace without and with its `sync -f`, descriptors reused after close, unfinished calls, renames, removals, and on Linux with strace a real command.

`python -m unittest discover -s tests -p test_exp11_analysis.py` covers EXP-11's analysis with the analysis venv's matplotlib (skipped without it): `analysis/exp04.crossover` against the full copy and against a named baseline; the pooled choice of each envelope's arm (ties within 0.05 s, main-block runs only, void runs, A4's and A5's excluded arms, copies the tool itself aborted, errors of the harness and of unknown kind, A6); the ratios with `R_adj` and the bound's gate (A3); the HA verdict's rows in order (Out with and without its qualifier, Confirmed, Bound missing, Undecided) and its flags (fragile, near the line, ratios that decide only together, with void runs); check (a) judged again with the smoke run's idle noise; `pgdelta/`'s diff beside the drift gates; crossovers against a chosen baseline, provisional ones, and L3's "below 2"; the drift gates and L3 and L4 not judged when the session is shifted; and dry runs of the whole script on a synthetic session (`tests/exp11_smoke_data.py`, written through `measure.write_csv`) in the full and the smoke shape, with void runs and with durability failures, into a temporary directory. It also covers `report.py` on a T2 summary.

`python -m unittest discover -s tests -p test_exp11_integration.py` covers EXP-11's blocks together: the two run lines, read from EXP-11's Run plan as the shell expands them, through `remote.py start` and `parse_args` (known methods, the CRC32C guard, the block options, check (a)'s idle noise, and 25 and 24 arm-speed pairs per repeat: 250 + 80 + 18 runs in hot and append, 120 in uniform); every arm run and verified by its own block; a dropped range through `run_method` and the analysis, which excludes parcopy from every baseline (A4); the syscall audit's records by arm (`summary --json`) and the analysis reading them from a results directory or that JSON, E13's clause included; parcopy's fsync count kept apart from check (a) by the analysis, with check (a) void too; and the receiver's listening time in the CSV and the phases figure.

`python -m unittest discover -s tests -p test_exp12_load.py` covers T10a's harness without a VM: pgbench's log and `-P 1` lines as PostgreSQL 17 writes them, the commits counted per client, the load's command line and scripts; the settle gate; the consistent cut mapped across a capacity run and a load run by their offsets, and coverage; the oracle's verdicts, each negative control through its own check, and a live output's status; `fincore`, the cache flag, the sampler's groups from real process titles, `schedstat`, busy CPU without steal and the end point line; every new arm's tier, `PROC_SCOPE`, `run_phases` and `verify` path, stats path, whether it stops the source and whether check (b) applies, the per-end jobs; with every VM step stubbed (`LoadStub`, which records each command with its time), a whole trial in EXP-12's order, every live output passing its oracle with its cut and coverage, the proxies reading the snapshot without stopping the source, nothing on the source in any settle, window or tail, check (c) (each arm's flush inside its phase, dest's verification after the last read of (a) and (b)), the source's half of each verification before the next settle, a second eviction, a check that times out (unverified) and an error from the copy (wrong), an unsettled settle, a dead load voiding the run before it, `run.json`'s pins of each new arm's command line, the clock mapping with a known offset, `execute`'s refusals; the run lines, split as a POSIX shell splits them; the agent's new commands on temporary files, a fake `/proc` and loopback. With PostgreSQL 17's binaries (`PGDELTA_TEST_PG_BIN`, or Homebrew's), on a local cluster: loaddb, the scripts' invariant under concurrent clients, `backup-hold` closing on stdin, two schema dumps of one state hashing equal, the snapshot, and a good copy under the load passing every check while each of the seven negative controls fails through its own.

`python -m unittest discover -s tests -p test_exp12_analysis.py` covers EXP-12's analysis with the analysis venv's numpy and matplotlib (skipped without them): pgbench's lines read fast and line by line; W and D on windows with known answers, and a 2-s pause at ρ; the block-bootstrap null with a known spread, each run at its own T, "measurable", the bound and δ centred for two cells of different T; every row of every rule (L1-L5, R1-R3, decisions 1 and 2 in their orders, A2-A6, A13, near the line, fragile, the short cells); the sampler's view, the settle gate's log part and the clock mapping; smoke item 15's projection; and dry runs of the whole script on a synthetic session from a queue model (`tests/exp12_smoke_data.py`), in the sweep's and the smoke run's shapes, with a wrong output, unsettled settles, durability failures, a dead load, compressed and missing logs, into a temporary directory.

`python -m unittest discover -s tests -p test_exp12_integration.py` covers T10a's blocks together: the run lines exactly as EXP-12 writes them (`R_BUSY=0`, `R_MOD=0`) through `remote.py start`, `parse_args` and the analysis's parser, the registered 117 runs, and `execute` refusing the zero rates before it touches anything; this section naming every option of `run_exp04.py`'s EXP-12 group, of the run line and of the analysis; and the stubbed trial with its load at the rate asked for and `pause2`'s stop in its logs, read by the analysis: every loaded run has numbers, the stall lands in `pause2`'s windows and nowhere else, and the script writes its data and figures from the harness's records.

pgdelta's prefetch itself is tested in `tests/test_pgdelta_prefetch.py`.
