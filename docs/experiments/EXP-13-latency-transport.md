# EXP-13: What changes at DR round-trip times? (T12, latency and transport)

Status: **pre-registered October 24, 2026, revised after an independent review and a check of the revision; not yet run; the harness code is not built.** This is test T12 of the [decision ledger](../DECISIONS.md#t12-latency-and-transport). It merges two independent drafts: one written for realism (which arms an operator running Omni's DR would face, and the smallest design that decides them), one for measurement validity (what each clock holds, and what would make a number wrong). Where they disagreed, the choice and its reason are under [Where the two drafts differed](#where-the-two-drafts-differed). An independent review then checked the merged draft against the ledger, EXP-09, EXP-11 and the harness code, before anything was built: 12 findings, 4 major. Every change it led to is listed under [Revisions before running](#revisions-before-running-october-24-2026), with the finding it answers. A second reviewer then checked that revision: 13 findings, none major. Its changes are under [After a check of the revision](#after-a-check-of-the-revision). The ledger's registered question, prediction and rule are quoted word for word and judged as registered. Everything marked "added here" is new. Where the arms depart from the ledger's, the reason is under [Changes from the ledger](#changes-from-the-ledger-rule-8), as rule 8 asks.

## Question

[EXP-11](EXP-11-full-copy-baseline.md) found v1's path Out on fast links. A range-parallel copier built as a bound (`parcopy8`) made a durable copy in 1.88 s unthrottled, and v1 took 1.64–1.65 times as long for local changes. By EXP-09's rule, v1 now targets DR's slower links first. At 0.1 ms round-trip time, v1 beat every copy measured below about 6.5 Gbit/s for local changes and 5.0 for uniform. At 2 Gbit/s it took 3.11–3.19 s (local) and 5.31 s (uniform), against the bound's 9.30–9.33 s.

Omni's DR secondaries sit in another region (O1), 20–120 ms away. No test has added latency. Three things could change there:
1. **pgdelta's protocol waits on round trips.** The sender hashes a file, asks for the page digests of its changed nodes, and waits for them while it hashes at most 4 files ahead (D29, D30; `DEFAULT_WINDOW = 4` in `pgdelta/sender.py`, with no option to change it). In EXP-11's hot and uniform runs, 19 files with a basis changed (from the raw run records). The connect, HELLO and ACCEPT, and the final COMPLETE add more round trips.
2. **One TCP stream carries at most its window per round trip.** On these VMs (Debian 12, kernel 6.1, CUBIC; `tcp_rmem` up to 6 MiB, of which half is window; `tcp_wmem` up to 4 MiB; all in EXP-11's `run.json`), that is about 3 MiB: about 0.42 Gbit/s at 60 ms and 0.21 at 120. v1 sends over one stream (D36); the bound sends over 8. At 0.1 ms none of this showed.
3. **O6 requires TLS for pgdelta,** which it doesn't have yet. TLS 1.3 adds a round trip to the start and costs CPU per byte.

On the same two VMs, in one session:
1. **Does today's protocol hold at DR latency?** The ledger's prediction: with a window of 4, pgdelta adds 2 s or less at 60 ms, and a single-stream full copy slows down more.
2. **Does v1's path still beat the fastest durable full copy at DR latency** (the DR verdict, added here)? On hosts as they come, and with TCP buffers raised on both hosts.
3. **One stream or several?** Where does one stream's window limit v1 and the full copies?
4. **Does the request window matter** (1, 4 or 16)? Does the high-churn fallback help at 10% changes?
5. **What does TLS cost pgdelta, and is SSH an alternative?**

It decides the ledger's T12 prediction and rule, D36 (one TCP stream, default buffers), D29 (the window) and D22 (the high-churn fallback). It moves D30 and D19 (round trips at WAN latency), D37 and D14 under O6 (TLS; SSH), D72 (no added latency), and D03's and D78's crossovers, which so far hold only at 0.1 ms. It also runs v1's path (D58, D85) in a third session.

**What would change the plan** (the full rules are under [Added rules](#added-rules)). The first row comes before the others (revised):

| If | Then |
|---|---|
| pgdelta slows down more than one `pg_basebackup` stream at any r of 20, 60 and 120 ms at 2 Gbit/s (the ledger's rule, R1) **(expected, on tuned hosts)** | As registered: deeper pipelining before any WAN claim. Every verdict row for v1 is provisional, and this session makes no DR claim for v1. The timers say how much of v1's excess pipelining could remove; that lifts nothing |
| v1 beats the bound at 20, 60 and 120 ms on hosts as they come | Today's protocol would make the DR claim at 2 Gbit/s on these VMs, if R1 doesn't fire. D36 stands only if row 1 holds at 60 and 120 ms and neither is provisional (A2); otherwise it stays C. T8 next, unchanged |
| v1 loses for scattered changes on hosts as they come, but wins once TCP buffers are raised **(expected)** | Default TCP buffers cost the DR case (D36 contradicted, whether or not R1 fires; `ss` says whether one stream's window is why). v1 needs parallel page streams, or Omni's nodes need a TCP buffer setting: an owner decision, and a T8 build item |
| v1 loses even with raised buffers, and the timers put the loss in pgdelta's idle waits | The protocol's round trips cost the DR case: deeper pipelining before any WAN claim |
| TLS through a proxy costs more than max(0.3 s, 5%) | Native TLS gets its own timed arm in T8 before any WAN claim with TLS |
| A window of 16 saves 0.3 s or more in one workload and costs at most 0.1 s in the other | The default window becomes 16 |
| The fallback saves time at 10% without costing at 1% | It is turned on (not expected) |

## Setup

### Machines and software

- Two `n2-standard-8` VMs in `us-central1-a`, labelled `purpose=pg-delta-benchmark`, as EXP-08 to EXP-11. At most two exist at any time. About 1 USD per hour for the pair.
- Four local NVMe SSDs in RAID 0 at `/data`, ext4 in ordered mode, Debian 12. The runs refuse to start otherwise (EXP-11's preflight).
- PostgreSQL 17 from PGDG; `pg_probackup` 2.5.16 on both VMs; Debian's OpenSSH, OpenSSL, stunnel4 and strace. Every version goes into `run.json`. So do the interface's MTU (1460 in EXP-11), driver and queue count, and every TCP sysctl named below.
- pgdelta at EXP-11's code, plus this test's changes: a `--window` switch on `send`, and protocol timers in both ends' statistics, with each of the sender's waits split into busy and idle parts (see [What it needs built](#what-it-needs-built-in-the-harness)). `DEPLOYED.json` records the diff of `pgdelta/` from EXP-11's deployed commit.

### Data and trials

- pgbench scale 70 (2.1 GB) with EXP-01's `events` table and EXP-01's seed, as EXP-09 to EXP-11.
- Three trials, each with its own cluster and B0, in this order:
  - **uniform 1%**, the deciding trial for one stream: 609 MB of pages on the wire (EXP-11);
  - **hot 1%**, local changes: 29 MB, where round trips are most of what latency adds;
  - **uniform 10%** (added here): about 1.13 GB (EXP-02's pgdelta sent 1,127 MB for uniform 10% and 608 MB for uniform 1%, where v1 sent 609 MB in EXP-11; the smoke run records the real figure). After a DR failover the old primary may be hours behind, so heavier divergence is a realistic DR case. It is also the fallback's case.
- A full copy moves 2.12 GB in every trial.
- Append is left out. It ran within 0.1 s of hot in EXP-09, EXP-10 and EXP-11, with the same 19 changed files.
- **Manifest checksums (rule 7):** CRC32C for every `pg_basebackup`, B0 included, as EXP-11, with its guard.

### The emulated WAN (added here)

**Conditions.** A condition is a link rate, an added round-trip time r and a host TCP setting.

| Variable | Values | Why |
|---|---|---|
| Added round-trip time r | 0 (the anchor), 20, 60 and 120 ms | The ledger's three, plus an anchor that repeats EXP-11's 2 Gbit/s cell in this session, through the same emulator. Every "added by latency" number is within the session. The zone's own 0.1 ms comes on top |
| Link rate | 2 Gbit/s everywhere; 1 Gbit/s too, for uniform 1% on default hosts at 60 and 120 ms | 2 Gbit/s is below EXP-11's 0.1 ms crossovers, and EXP-09 and EXP-11 measured every arm there. 1 Gbit/s brackets uniform's crossover where one default stream is expected to lose at 2 Gbit/s |
| Host TCP setting | **default**: Debian's, recorded. **tuned**: the `net.ipv4.tcp_rmem` and `tcp_wmem` maxima raised to 128 MiB on both VMs | One default stream is window-limited at 60 ms. Tuned is a host setting, like EXP-11's `full_wb`; whether Omni's nodes can set it is unknown |

Google caps traffic over external IPs at 3 Gbit/s per flow, and limits inter-region traffic by a per-project quota ([Omni notes](../research/notes/alloydb-omni.md)). At 1–2 Gbit/s the shaped link binds, not a per-flow cap.

**The conditions run:**

| | anchor | 20 ms | 60 ms | 120 ms |
|---|---|---|---|---|
| default, 2 Gbit/s | all trials | all trials | all trials | uniform 1%, hot |
| tuned, 2 Gbit/s | all trials, v1 and `full` only (revised) | – | all trials | all trials |
| default, 1 Gbit/s | – | – | uniform 1% | uniform 1% |

- **"The anchor" alone means the default anchor** (2 Gbit/s, r = 0, default hosts). The tuned anchor is always named (revised after the check).
- **The tuned anchor runs v1 and `full` only (revised).** It gives the tuned Δs that L1, L2 and R1 read an anchor of their own. At 0.1 ms the bandwidth-delay product is 25 KB, far below either setting's buffers, so the two anchors should agree (E1); A17 (ii) reports the gap. Its runs aren't in any ratio.
- **No tuned host at 20 ms.** At 20 ms one default stream carries about 1.0–1.3 Gbit/s and eight carry more than the link, so tuning can only help v1 there.
- **Uniform 10% skips 120 ms on default hosts.** v1 is window-limited there. If uniform 1% (609 MB) is Out there, uniform 10% (1.13 GB through the same stream, against the same copies) is Out too, by arithmetic. If uniform 1% isn't Out there, uniform 10% at 120 ms is "not measured".
- **No 1 Gbit/s on tuned hosts or for hot.** On tuned hosts v1 and the copies are all link-bound, and v1 sends fewer bytes, so a slower link only favours v1. Hot's v1 time doesn't depend on the rate (EXP-09, EXP-11).

**The tree, on both VMs' interface** (`infra/gcp/wan.sh`, new; `throttle.sh` unchanged):
- a `prio` root with two bands;
- a `u32` filter that sends to the delayed band only traffic to the peer VM, except TCP port 2222 as source or destination port. Everything else (the harness's control path, the Mac's SSH, the metadata server) goes to the plain band, neither delayed nor shaped;
- the delayed band: `netem delay r/2 limit 1000000`, with a `tbf` child at the condition's rate. The tbf uses `throttle.sh`'s burst (10 ms of tokens, at least 128 KiB) and a queue of max(50 ms, r) of the rate.

Notes on the placement:
- **r/2 on each VM,** so data and acknowledgements, and requests in either direction, are delayed alike. `pg_basebackup` pulls from the source; pgdelta and `parcopy` push to dest.
- **netem before tbf.** Packets wait r/2 in netem's delay line, then queue at the shaper. So the delay line stays separate from the bottleneck queue, and the queue stays bounded. A tbf above netem would make one queue and inflate the round trip. netem's limit is far above any window run here, so netem itself should never drop.
- **netem counts its child's drops as its own (revised).** In kernel 6.1, when the tbf refuses a packet netem hands it, netem's own drop counter grows too, and so does the `prio` root's. So the emulator's own overflow is netem's drops less tbf's, both read from the same `tc -s -j` snapshot, and netem's backlog, sampled every 0.5 s, must stay below its limit.
- **Drops at the shaper appear only from r > 0 (revised).** netem orphans a packet only when a delay is set. At the anchor (delay 0) TCP Small Queues still bound what a sender puts in the qdisc, so the tbf hardly ever drops, as in EXP-11. From r > 0 nothing bounds it but the window: wherever no window limits the flows (every tuned bulk run; default `parcopy8` at 20 ms, and at 1 Gbit/s and 60 ms), the tbf queue overflows and drops, which is how CUBIC finds the rate.
- **The bottleneck queue is max(50 ms, r).** One CUBIC flow keeps the link full after a loss only if the queue holds about 0.43 of the bandwidth-delay product. A fixed 50 ms would fall below that at 120 ms and penalise one stream for the queue, not its window. This favours one stream, so any gain from parallel streams measured here comes from windows alone. On tuned hosts a full queue adds up to r to round trips during bulk sends, as a deep real buffer would.
- **No jitter, loss or reordering.** Constant delay only.
- **The anchor uses the same tree with r = 0,** so a change of tree can't pass for a change of latency. Smoke item 2 compares it with EXP-11's plain tbf.
- Every untimed step outside a condition's visit (cluster build, B0, the workload, the trial's end) runs with the tree off.

**The control path stays outside the emulated WAN.** Each run issues 15–25 control commands from dest to the source (snapshots, cache drops, stops, digests, the sender's launch). Over the delayed path each would cost a few round trips: 2–4 s of untimed overhead per run at 60–120 ms. Worse, the senders of pgdelta and `parcopy` are launched over that path inside the clock, so its round trips would be a harness artifact. So the source's sshd also listens on port 2222, and the harness's multiplexed connection uses it. The methods' own connections all cross the delayed band: pgdelta's and `parcopy`'s data connections, `pg_basebackup`'s two libpq connections, `pg_probackup`'s SSH connections (port 22), the stunnel pair, the link tests, the pings and the SSH probe. A real deployment must trigger its sender across the WAN, at least half a round trip; `R_trig` (below) prices a full one.

**Checked at every visit to a condition, before any run:**

| Check | Pass |
|---|---|
| The tree, both VMs | `tc -s` parses to the intended tree: the filter, netem with r/2 and its limit, tbf with its rate and queue |
| 20 pings to the peer, 0.05 s apart (as root) | minimum in [r, r + 1 ms]; median at most the minimum + 2 ms |
| 5 no-ops over the control path | each within 5 ms of the same check at the anchor |
| TCP sysctls, both VMs | the condition's setting (A11) |

**Checked in every run:**
- **netem's own drops (revised):** its drop counter less its child tbf's, from the same snapshot, grows by **0** on both VMs, and its largest sampled backlog stays below its limit. Either one failing is the emulator overflowing, not congestion.
- netem's sent bytes on the source grow by at least 0.98 × the run's wire bytes from source to dest, so the run's data went through the delay.
- The sending socket's smallest round trip (`minrtt`, from the `ss` sampler below) is in [r, r + 2 ms].
- tbf drops and overlimits, and TCP retransmissions (`nstat`) on both VMs: recorded, not judged. Drops at the shaper are how CUBIC finds the rate when no window limits it.
- **TCP metrics are flushed** (`ip tcp_metrics flush all`, both VMs) before every run and every link test. Otherwise Linux reuses a slow-start threshold cached from the last connection to the same host, and a run would depend on the one before it.

### TCP settings (added here)

| Setting | `net.ipv4.tcp_rmem` | `net.ipv4.tcp_wmem` | Largest window (kernel 6.1, `tcp_adv_win_scale` 1) |
|---|---|---|---|
| **default** (Debian's; in EXP-11's `run.json`) | 4096 131072 6291456 | 4096 16384 4194304 | about 3 MiB |
| **tuned** | 4096 131072 134217728 | 4096 16384 134217728 | about 64 MiB |

- Only the two maxima change. CUBIC, `tcp_adv_win_scale`, `tcp_slow_start_after_idle` (1), `net.core.rmem_max` and the rest stay at Debian's values, recorded per run.
- 64 MiB covers the largest bandwidth-delay product run (2 Gbit/s × 120 ms = 30 MB) with room for the bottleneck queue.
- The visit sets the condition's values on both VMs. A guard before every run requires them on both. The reset writes the recorded defaults back, in a `finally` too, as EXP-11's `full_wb` did. A unit test forces an exception and checks the reset.
- **Why pgdelta can't fix this alone.** An application that sets `SO_RCVBUF` itself loses autotuning, and is capped at twice `net.core.rmem_max` (212,992 bytes by default, so about 416 KiB), less than autotuning's 3 MiB. Without root, an application's only lever is more streams. No arm here sets its socket buffers, so autotuning applies to all of them.
- **The send buffer grows too.** With `tcp_wmem` at 128 MiB, the sender's socket can hold tens of MB that `send()` has accepted but TCP hasn't sent. Anything written after them, a leaf request among them, waits behind them. The busy and idle split of pgdelta's waits (below) keeps that time out of the protocol's waits (revised).

### TLS: measured through a proxy, not built into pgdelta (added here)

**Decision.** `v1_tls` runs pgdelta through a stunnel pair:
- on the source, stunnel in client mode listens on localhost port 7001, and the sender connects there;
- it opens TLS 1.3 to dest's stunnel on port 7443, which forwards to the receiver listening on localhost port 7000. The receiver is reachable only through stunnel;
- both ends present certificates from a per-session CA (mutual authentication);
- TLS 1.3 only, with OpenSSL's default suites; `TCP_NODELAY` on both of each stunnel's sockets;
- **stunnel's default `TIMEOUTclose` (revised).** pgdelta's receiver half-closes after its COMPLETE and drains until the sender closes (up to 2 s). With the default, dest's stunnel waits for the source's close_notify, which the source's stunnel sends when the sender closes. So the receiver's drain ends about one r after publication, as in clear. A timeout of 0 would close the receiver's socket at once and end the run about r/2 early;
- stunnel logs each connection's protocol and suite. Both stunnels start before the clock and stop after it, like EXP-04's rsync daemon. The TCP connect and the TLS handshake across the WAN happen inside the clock.
- **The proxy lengthens the receiver's prefetch (revised).** In server mode stunnel connects to the receiver only after its TLS handshake. The receiver reads B ahead (`--prefetch-basis on`) until it accepts, so `v1_tls`'s receiver prefetches for about one round trip longer than v1's, which native TLS wouldn't give. The receiver's prefetched bytes are recorded in both arms, and A6 prices the difference.

**Why a proxy, and why not build TLS into pgdelta now:**
- At DR rates, TLS's costs that matter are one handshake round trip and encryption CPU. A proxy pays both. It also adds a localhost hop on each end, which only adds to v1's time. Its one advantage, the longer prefetch, is priced (A6).
- **The window across the WAN is still the kernel's.** stunnel sets no socket buffers, so its WAN socket autotunes exactly as pgdelta's own does. An SSH tunnel is different: its channel window (2 MiB in OpenSSH) caps a connection whatever TCP allows. Smoke item 12 checks this: a link test through the stunnel pair must be within 10% of a direct one at 60 ms on default hosts, and reach 0.9 of the link at 120 ms on tuned hosts.
- Building TLS into pgdelta is product work with a real design question. pgdelta's connection is read by one thread and written by another at the same time (D34; the sender's `Inbox` thread), and one OpenSSL connection is not safe to use from two threads at once without care. That belongs with T8's transport, where v1's connection is rebuilt anyway.
- It would delay T12 by about a day, for a cost predicted at 0.5 s or less.

The proxy also encrypts pgdelta's hash key, which crosses the link in HELLO (D14's gray area). Native TLS closes that gray area in T8. A6 says what happens if the proxy costs more than predicted. The CA, keys and certificates are made on dest at session start, copied to the source over the control path, and never leave the VMs or enter the repository.

**v1's main arm and the baselines run in clear.** The verdict and the registered judgments use v1 in clear, today's protocol, like for like with the baselines, which also send in clear (rule 4). TLS's cost is measured beside it at two conditions, and the verdict is restated with it where it ran (`R_tls`). Omni recommends TLS for all connections, but no Omni document says its replication traffic is encrypted ([Omni notes](../research/notes/alloydb-omni.md)). `R_tls` therefore carries the note "v1 pays TLS through a proxy; the baselines run in clear", which favours the baselines.

### Repeats and order

- **v1 and its side arms:** 5 repeats per condition, in their trial.
- **The baselines,** `full` and `parcopy8`, run in repeats 2 and 4 of each trial and are **pooled over the trials**: 4–6 runs per 2 Gbit/s condition. A full copy's work doesn't depend on the workload: at 2 Gbit/s EXP-11's medians came out within 0.05 s across the three workloads (`full` 10.27–10.32 s, `parcopy8` 9.30–9.33 s). Pooling also spreads them over the session. At 1 Gbit/s, `parcopy8` runs in all 5 repeats of uniform 1%, the only trial that visits it. `parcopy16` runs in repeats 2 and 4 of uniform 1% and hot at 120 ms, 2 Gbit/s, default: 4 runs, pooled (revised). At the tuned anchor only `full` runs as a baseline, in repeats 2 and 4: 6 runs, pooled.
- **Order.** Trials run uniform 1%, hot, then uniform 10%, so the deciding trial comes first. Within a trial, each repeat visits its conditions in a seeded random order, and each visit runs its arms in a seeded random order (seeds 131, 132 and 133).

**Order within a trial:**
1. Tree off. Cluster and dataset, B0 (unthrottled, as EXP-11), the workload and its `CHECKPOINT`.
2. In every trial (revised after the check): the start reference digest (a clean stop, `agent.py datadir-digest`, a restart; untimed), then EXP-11's probes (the durable write floor, the cold read floor, the single reader, the backup-mode bracket; untimed). The harness takes both in every trial of a run that selects one of EXP-11's arms (`measure.T2_METHODS`), and every invocation here selects `parcopy8`. They run with the tree off and Debian's sysctls. They are reported and decide nothing, except the digests' part in A15's void rule for `probackup8`.
3. In uniform 1% only: a one-stream link test at 1 Gbit/s through the tree with r = 0. Its rate is the crossovers' x at 1 Gbit/s.
4. The repeats. Each visit: set the tree and the TCP sysctls on both VMs; the visit's checks; a TCP metrics flush; a one-stream link test from source to dest (6 s, the first 3 s a warmup; the steady rate is the median of the last 3 one-second rates). At a trial's first visit to a condition, also: one stream from dest to source, 8 streams from source to dest, and at 120 ms, 2 Gbit/s, default, 16 streams too (revised); and in uniform 1%, the SSH probe (at 20, 60 and 120 ms, both settings) and a one-stream link test through the stunnel pair (at the anchor, 60 ms default and 120 ms tuned). Then the arms, each preceded by the guard and a metrics flush.
5. Tree off, default sysctls.
6. In every trial: the durable write floor again and the end reference digest (revised after the check). In uniform 1%, then the source's logical digest, which settles `probackup8`'s pending checks.
7. EXP-01's row-level ground truth; B0's full digest.

The probes set no floor here. At 2 Gbit/s and below the link sets the floor (EXP-11's floor at 2 Gbit/s was the link time, 8.83 s). They are kept because the harness runs them with EXP-11's arms, and so each trial's steps stay EXP-11's, which A10's drift gates compare against (revised after the check). The bracket is reported beside `R_adj`, as in EXP-11.

### Cache state at the start of every run

As EXP-11's main block: both VMs' page caches dropped once, after the untimed set-up; `Dirty` and `Writeback` recorded on dest before and after the clock; v1's B a fresh `cp -a --reflink=never` of B0, synced before the drop; full copies' output directory absent. The source is running and idle for `full`, `full_nosync` and `probackup8`. It is stopped cleanly before the drop for v1's arms and the `parcopy` arms, and restarted after the run. No warm-source block.

### What the clock includes

The clock is `perf_counter` on dest. Each run is one timed phase with one counter window, as in EXP-11.

| Component | `full`, `full_nosync` | `probackup8` | `parcopy8`, `parcopy16` | v1's arms |
|---|---|---|---|---|
| Launch | local on dest | local on dest | receiver local; sender over the control path (undelayed) | as `parcopy8`; for `v1_tls`, both stunnels already running |
| Connection set-up across the WAN | in: TCP, startup, `IDENTIFY_SYSTEM`, `BASE_BACKUP`, and a second connection for WAL | in: each SSH connection's TCP, key exchange, authentication and channel | in: a control connection and 8 or 16 data connections | in: one TCP connection, then HELLO and ACCEPT. `v1_tls`: stunnel's TCP connect and the TLS 1.3 handshake |
| Backup mode | in, with its round trips | in | out: stopped source | out: stopped source |
| The method's own round trips | its replication protocol's (few) | its own protocol's (not timed inside) | none after the start | the basis digests' half round trip, up to 19 leaf requests, the final COMPLETE |
| Slow start | in | in | in, 8 or 16 streams | in, at every burst after an idle longer than the retransmit timeout |
| Flushing | its default `fsync` method | its own fsyncs, then `sync -f` | write-back every 8 MiB, then parallel fsyncs | pgdelta's, for the pages it writes |
| Encryption | none | SSH (`aes128-gcm@openssh.com`) | none | none; `v1_tls`: TLS 1.3 |
| End | `pg_basebackup` exits | `sync -f` returns | both ends exit | both ends exit; the receiver's drain after COMPLETE (about one r) included, through the proxy too |
| Out | the cache drop, set-up, the tree, the sysctls, the metrics flush, the link tests, verification | the same | the same | the same |

**Views of the same runs (added here):**
- `total_s`: the registered clock, as EXP-11.
- `R_trig`: for arms launched over the undelayed control path (v1's arms, the `parcopy` arms), `total_s` plus one r. That prices the trigger a real deployment would pay. It overstates it: half a round trip is the minimum.
- `publish_s` (v1's arms): from the receiver's launch to its publication. The clock goes on for about one more round trip while the sender learns of COMPLETE and closes. Reported only.
- `R_adj`: v1 plus `full`'s backup-mode adder (its checkpoint wait, plus its wait from the WAL end point to its sync), as EXP-11, **taken at the default anchor only**. At latency `full`'s timestamps hold its own WAN round trips, which a live v1 reading its local server wouldn't pay. It still leaves out a live v1's WAL streaming, `backup_label` and recovery (T8).
- `usable_s` is **not reported.** EXP-11 found that the harness's untimed verification flushes the output before its start, so the column can't say what it claims.

### Durability

EXP-11's definition, directory rule and checks apply unchanged:
- (a) `Dirty + Writeback` on dest rises by at most the run's limit over the clock: max(4 MiB, the idle noise of the shortest window length that covers the run's clock). The smoke run measures dest's idle noise over 20 windows of 5 s, 6 of 30 s and 3 of 120 s. A length's noise is the largest increase over windows of that length or shorter. So a v1 run of up to 5 s is held to the 5 s noise, one of up to 30 s to the 30 s noise, and `full`'s runs of 41–102 s to the 120 s noise. A run longer than 120 s takes the 120 s noise, flagged. Each run's limit and window length are in its `durability` record (revised after the check);
- (b) for full copies, dest's md array writes at least 0.98 × the output's bytes inside the window;
- each `parcopy` run's `fsync` count equals its inventory's files plus directories plus one;
- **negative control:** one `full_nosync` run at the anchor, in each trial's first repeat, must fail (a) by at least 1.5 GB (A14).

`full`, `parcopy8` and `pg_probackup` with its `sync -f` are EXP-11's arms, whose syscall audit passed, and their write paths don't change with the network. `parcopy16` is the same code with 16 streams; EXP-11's `parcopy` already fsyncs with 16 threads at any stream count. The audit (EXP-11's) runs again in the smoke run, at scale 5, for v1 and `v1_tls`: v1's code changed (the timers), and `v1_tls`'s path did (the proxy). A `full` stream that outlasts 30 s lets the kernel write back its early files during the stream (`vm.dirty_expire_centisecs` 3000), so its final sync shrinks. That is real behaviour at Debian's defaults, and checks (a) and (b) judge it like any other flush.

### Verification (untimed, every run)

| Arm | Contents | Started? |
|---|---|---|
| `full` | `pg_verifybackup` with the CRC32C manifest | yes: recovered and queried, as EXP-11 |
| `probackup8` | started from what the tool wrote, with no WAL fetched; its per-table logical digest against the source's at the trial's end | yes |
| `parcopy8`, `parcopy16` | `verify_direct`: every selected file byte-identical to the stopped source; no inode shared with B0; the `fsync` count | yes |
| v1's arms | EXP-09's check: byte-identical to the stopped source's selection, no in-place leftovers | yes |
| `v1_tls` | also: stunnel's log shows the connection, TLS 1.3 and its suite | – |
| every arm | B0 unchanged (stat snapshot per run, full digest per trial) | – |

**One rule changed from EXP-11 (A15).** EXP-11 voided a `pg_probackup` logical difference whenever the start and end reference digests differed. They differed in every trial, because system catalogs changed (most likely autovacuum), so its A4's logical leg could never fire. Here a run is void only if a **user-table file** (`pgbench_*`, `events`, their forks and indexes) differs between the two reference digests. Catalog differences are reported by file class.

A run that fails verification is excluded and reported. Nothing is rerun.

### Measured

**In every run,** EXP-11's fields (`total_s`, `total_no_ssh_s`, wire bytes each way at the interface, VM-wide CPU on both VMs, per-process rusage, dest's disk counters, `Dirty`/`Writeback`, each tool's own phase timers), plus (added here):
- the condition: rate, r, host setting, and both VMs' TCP sysctls in force; the visit's ping minimum and one-stream rate;
- netem and tbf counters on both VMs, and netem's own drops (netem's less tbf's); `nstat` TCP counters (`TcpRetransSegs`, `TCPLostRetransmit`, `TCPTimeouts`) on both VMs;
- **an `ss -tin` sampler on the source,** every 0.5 s, for its sockets to dest: per connection, `minrtt`, `cwnd`, `busy`, `rwnd_limited`, `sndbuf_limited`, `retrans`, `notsent` and `unacked` (the last two added after the check, for stunnel's WAN socket). Beside it, a `tc -s -j` sample on both VMs every 0.5 s, for netem's backlog (revised). Both start before the clock and stop after it, the same in every arm. The `ss` sampler's last sample per connection is that connection's reading;
- **pgdelta's protocol timers** (built here; rule 6): the sender's TCP connect (`connect_s`, added after the check), its waits for ACCEPT (`accept_wait_s`), for basis digests (`basis_wait_s`) and for leaf digests (`leaf_wait_s`, with the number of waits over 1 ms); its time blocked sending page data (`page_send_s`); its wait for COMPLETE (`complete_wait_s`); its leaf requests. The receiver's wait from listening to HELLO (`hello_wait_s`), its main loop's time blocked reading (`recv_wait_s`), `publish_s`, and its prefetched bytes and seconds (already in its statistics);
- **each of the sender's waits split into busy and idle (revised).** During a wait the sender wakes every 1 ms and reads its socket's unsent bytes (`SIOCOUTQNSD`) and its bytes not yet acknowledged (`SIOCOUTQ`). A slice is **busy** when some bytes are unsent, or when the bytes in flight (`SIOCOUTQ` less `SIOCOUTQNSD`) exceed what the path holds without queueing: the visit's one-stream steady rate times its ping minimum, which the harness passes to the sender. Busy means our own data was still queued ahead in the socket or at the shaper. Otherwise the slice is **idle**. Each wait gets `*_busy_s` and `*_idle_s`, which add up to it. **The split doesn't hold for `v1_tls`** (revised after the check): there the sender's socket is the localhost hop to stunnel, and bytes waiting in stunnel's buffers and its WAN socket don't show in it, so a wait can read idle while the link is busy. For `v1_tls` the split is recorded and never cited;
- the arm's window, fallback threshold and high-churn files; for `v1_tls`, stunnel's protocol and suite.

They are here because of an EXP-11 lesson: its write-up gave `pg_probackup`'s slowness a cause its timers didn't show. Here, a claim that a stream was window-limited must be shown by `rwnd_limited` plus `sndbuf_limited`, and a claim that pgdelta's round trips cost time must be shown by the idle parts of its waits (in clear only).

**Per visit:** the tree and its counters, the pings, the control no-ops, the one-stream link test with its per-second rates. **At a trial's first visit to a condition:** one stream from dest to source, 8 streams from source to dest (and 16 at 120 ms, 2 Gbit/s, default); in uniform 1%, the SSH probe and the stunnel link test where listed. **The SSH probe:** one SSH session channel from the source's sshd on port 22 (`aes128-gcm@openssh.com`, as EXP-11's `pg_probackup`), carrying `cat /dev/zero` to dest for 6 s; its rate over the last 3 s.

**Beside time (rule 3):** wire bytes; egress cost at list prices, from measured bytes (about $20 per TiB between North American regions and $51 from North America to Europe; [Omni notes](../research/notes/alloydb-omni.md)); source CPU, as a proxy only, since the source is idle.

## Arms

| Arm | Tier | What runs | Source | Conditions | Trials |
|---|---|---|---|---|---|
| **`v1`** (`pgdelta_direct_inplace`) | reference | EXP-11's v1 arm: 4 jobs on each end, `--source-kind datadir`, `--in-place`, `--fsync main`, `--prefetch-basis on`; window 4 (now pinned by `--window 4`); fallback off; in clear | stopped | every condition of its trial, the tuned anchor included | all |
| `v1_w1`, `v1_w16` | protocol | v1 with `--window 1` or `--window 16` | stopped | 120 ms, 2 Gbit/s, tuned | uniform 1%, hot |
| `v1_hc` | protocol | v1 with `--high-churn-node-fraction 0.5` | stopped | 120 ms, 2 Gbit/s, tuned | uniform 1%, uniform 10% |
| `v1_tls` | protocol (O6) | v1 through the stunnel pair | stopped | the anchor; 120 ms, 2 Gbit/s, tuned | uniform 1%, hot |
| `full` | Omni today | EXP-11's `pg_basebackup -Fp -X stream -c fast --manifest-checksums CRC32C --verbose`, one stream, `--sync-method=fsync`, in clear | running, idle | every 2 Gbit/s condition of its trial, the tuned anchor included; at 1 Gbit/s inferred (A16) | all (repeats 2, 4) |
| `parcopy8` | bound | EXP-11's bound unchanged: 64 MiB ranges over 8 streams, write-back every 8 MiB, in clear | stopped | every condition of its trial but the tuned anchor | all (repeats 2, 4; every repeat at 1 Gbit/s) |
| `parcopy16` (revised) | bound | `parcopy8` with 16 streams | stopped | 120 ms, 2 Gbit/s, default, where 8 default windows carry less than the link | uniform 1%, hot (repeats 2, 4) |
| `probackup8` | released (SSH) | EXP-11's `pg_probackup catchup -b FULL --stream --temp-slot -j 8` over SSH (port 22, delayed), then `sync -f`. Its WAL ticker now runs on the source over the Unix socket, so its pace doesn't change with r | running, idle | 60 ms, 2 Gbit/s, default | uniform 1% (repeats 1, 3, 5) |
| `full_nosync` | negative control | `full` with `--no-sync`; never a baseline | running, idle | the anchor | all (repeat 1) |

**Runs per trial:**

| Trial | Conditions | v1 | Side arms | `parcopy8` | `parcopy16` | `full` | Others | Runs |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| uniform 1% | 9: the seven at 2 Gbit/s (anchor and tuned anchor; 20, 60, 120 ms default; 60, 120 ms tuned), and 60 and 120 ms at 1 Gbit/s | 45 | 25 (`v1_w1`, `v1_w16`, `v1_hc`, `v1_tls`) | 22 | 2 | 14 | 4 (`probackup8` 3, `full_nosync` 1) | 112 |
| hot | 7: those at 2 Gbit/s | 35 | 20 (`v1_w1`, `v1_w16`, `v1_tls`) | 12 | 2 | 14 | 1 | 84 |
| uniform 10% | 6: anchor and tuned anchor; 20 and 60 ms default; 60 and 120 ms tuned | 30 | 5 (`v1_hc`) | 10 | – | 12 | 1 | 58 |
| **all** | | **110** | **50** | **44** | **4** | **40** | **6** | **254** |

**Why these cells.**
- **The side arms run at one cell.** 120 ms, 2 Gbit/s, tuned is where round trips weigh most and TCP's window doesn't hide them. It is the window's, the fallback's and TLS's handshake's best case for showing an effect; if they gain nothing there, they gain nothing anywhere run here. TLS also runs at the anchor, where only its CPU shows.
- **The fallback's threshold, 0.5.** With uniform changes, nearly every 1 MiB node of `pgbench_accounts` differs at 1% as at 10%. EXP-01 found 45.9% of its heap pages changed at 1%, so a 128-page node is untouched with probability about 0.54^128, effectively 0. So any threshold under about 0.99 fires at both fractions. This is written now.
- **The baselines.** `full` is what Omni documents for a DR re-seed when a rewind fails (O1). `parcopy8` is rule 3's fastest durable copy (EXP-11) and the bound. At 120 ms on default hosts, eight default windows carry 1.39–1.68 Gbit/s, less than the 2 Gbit/s link, so `parcopy8` is window-bound there; `parcopy16` keeps the bound link-bound at that cell (revised). `probackup8` is the only released parallel copier found (EXP-11). It runs only at 60 ms, the ledger's round trip, 3 times, as the released envelope's second arm there; it costs about 65–100 s per run with its checks.

### Considered and not run

| Option | Why not |
|---|---|
| 5 Gbit/s and up | Budget, and above Google's 3 Gbit/s per-flow cap on external IPs. DR's slower links come first; EXP-11 has the 0.1 ms crossovers. Crossovers above 2 Gbit/s are stated as "above 2" |
| 1 Gbit/s on tuned hosts or for hot; tuned hosts at 20 ms; tuned arms other than v1 and `full` at the anchor; uniform 10% at 120 ms on default hosts | See the conditions |
| `parcopy16` at other cells | Eight default windows fill the 2 Gbit/s link at 20 and 60 ms, and at 1 Gbit/s at 60 and 120 ms; tuned windows fill it at every r (E3). Only 120 ms, 2 Gbit/s, default needs more streams |
| Loss, jitter, reordering | Realistic values for Omni's DR paths aren't known, and each needs its own design, since loss interacts with CUBIC. Loss hurts one loss-based stream more than 8, so leaving it out favours v1 and `full` over `parcopy8` |
| BBR, fq pacing, `tcp_slow_start_after_idle = 0` | Not Debian's defaults. A second host setting would confound the tuned reading. The timers show slow start's effect instead |
| pgdelta over SSH (the ledger's "SSH") | O6 chose TLS. OpenSSH's channel window caps one connection below one default TCP stream from 60 ms (2 MiB against 3 MiB). The SSH probe measures that on both host settings without a pgdelta arm |
| An SSH tunnel as pgdelta's TLS | It would measure the tunnel's channel window, not pgdelta's |
| Native TLS in pgdelta | T8 (see the TLS decision) |
| `full` over TLS (`sslmode`), `parcopy8` over TLS | In clear a baseline can only be as fast or faster. Rule 4 is met by saying so. TLS's cost to `pg_basebackup` decides nothing for pgdelta |
| `parcopy` with 1 stream | The link tests at 1 and 8 streams isolate the stream count at the transport, with no disk or tool in the way |
| Parallel page streams in pgdelta | Not built; A2 decides whether to build it |
| v1 at 8 jobs | 4 jobs is the registered arm (EXP-09, EXP-11), and the worker count is T10b's. Where v1 is window-bound (uniform on default hosts), 8 jobs would change little |
| Append; hot 10% | Append ran within 0.1 s of hot in EXP-09 to EXP-11. Hot changes stay in a small region of each table, so the fallback's per-file node fraction stays low; uniform 10% is its case |
| A plain-tbf cell in the sweep | Smoke item 2 compares the tree at r = 0 with EXP-11's plain tbf |
| A real cross-region VM pair | One round trip per region pair, so no in-session comparison across round trips. A later check of the emulation |
| TPROC-C data, a live or busy source, beyond RAM, persistent disks | T4, T8 and T10a, T9 |

## Baselines, deltas and ratios (added here)

A cell is (trial, condition). Everything below uses runs that pass verification, durability and the network checks (A11).

**Baselines** at each condition c:
- **B_fast(c),** rule 3's fastest durable full copy measured: the faster of `full`, `parcopy8`, `parcopy16` (at 120 ms, 2 Gbit/s, default) and `probackup8` (at 60 ms, default, 2 Gbit/s), by pooled median over every trial's runs at c. Ties within 0.05 s go to the lower pooled maximum. A `parcopy` arm is expected everywhere.
- **B_rel(c):** the same without the `parcopy` arms: one `pg_basebackup` stream, and `probackup8` where it ran (flagged "3 runs").
- **F0(c):** `full`, what Omni runs today.
- At 1 Gbit/s `full` doesn't run (A16). Its pooled median at 2 Gbit/s stands in, labelled inferred.

**Added by latency.** Δ_a(c) is arm a's median at c minus its median at the anchor of the same host setting: within its trial for v1's arms, pooled for the baselines. Tuned conditions use the tuned anchor for v1 and `full` (revised); the Δs from the default anchor are reported beside them, not judged. `parcopy8`'s tuned Δs use the default anchor, labelled; no rule reads them. **Effective round trips** n_a(c) = Δ_a(c) / r are reported: how many round trips an arm spends, a summary that needs no model.

**Ratios,** per workload and condition, v1's median (5 runs, its trial) over the baseline's pooled median:
- `R_fast` (the verdict's), `R_rel`, `R_F0`, and `R_adj` as above;
- robustness views: `R_max` (maxima), `R_trial` (against the same trial's 2 baseline runs), `R_trig`;
- `R_tls`: `v1_tls` in place of v1, where it ran, with A6's reading of `v1_tls`: the slower of the measured and the priced one (revised after the check).

**v1's arm is fixed in advance.** v1 in clear, window 4, on default hosts is the first reading of every rule and of the verdict. Tuned hosts are the pre-declared second reading. Neither is chosen on the data it is judged on. No side arm (`v1_w16` among them) ever takes v1's place in a row (revised).

**Crossovers** (reported, not a verdict): D78's method, `analysis/exp04.crossover` with a baseline, wherever both 1 and 2 Gbit/s ran: uniform 1% on default hosts at 60 and 120 ms. x is the trial's zero-delay one-stream rate at each tbf rate (the default anchor's link tests at 2 Gbit/s; step 3's probe at 1 Gbit/s). Outside the bracket: "below 1" or "above 2". Where only 2 Gbit/s ran: "above 2" if `R_fast` < 1.00 there, "below 2" otherwise, not interpolated.

## Predictions and decision rules

Every prediction and rule is judged per workload on medians (5 runs for v1's arms; pooled runs for the baselines: 4–6, 5 for `parcopy8` at 1 Gbit/s, 4 for `parcopy16`, 3 for `probackup8`), with the maximum beside it (rule 6), unless it says otherwise.

### The ledger's registered text (verbatim)

> **Question:** what changes at 20, 60 and 120 ms round-trip time? Arms: pgdelta windows of 1, 4 and 16; one vs several TCP streams; SSH; 1% and 10% changes, with the high-churn fallback on and off.
>
> **Prediction:** with a window of 4, pgdelta adds 2 s or less per 2.1 GB at 60 ms. A single-stream full copy slows down more.
>
> **Proves it wrong:** if pgdelta slows down more than the full copy, the protocol needs deeper pipelining before any WAN claim.
>
> *Added October 24 (after T2): v1 now targets DR's links first (EXP-09's rule, after EXP-11's Out on fast links). By rule 3, the full-copy side should include a parallel copy (EXP-11's `parcopy8`) beside one stream: at 0.1 ms one TCP stream isn't window-limited, so EXP-11's crossovers (6.5 Gbit/s local, 5.0 uniform) may not hold at DR latency.*

### How they are judged here

| ID | Registered | How it is judged here | Expected here (added) |
|---|---|---|---|
| L1 | "with a window of 4, pgdelta adds 2 s or less per 2.1 GB at 60 ms" | Δ_v1 at 60 ms, 2 Gbit/s, default hosts: at most 2.0 s, in every workload run there (hot, uniform 1%, uniform 10%). "pgdelta" is v1's path (change 1). The tuned reading (from the tuned anchor) is given beside it, not judged | **Holds for hot** (+0.5 to +2.1 s, so it may fail at the top). **Fails for uniform 1%** (+9.8 to +13.1 s) and **uniform 10%** (+17 to +23 s): one default stream carries about 43–53 MB/s at 60 ms, so 609 MB takes 11–14 s. Tuned: hot +0.4 to +1.8 s, uniform 1% +0.8 to +2.1 s |
| L2 | "A single-stream full copy slows down more." | Δ_full > Δ_v1 at 60 ms, 2 Gbit/s, default hosts, in every workload run there. The tuned reading (from the tuned anchor) beside it | **Holds** on default hosts: `full` +31 to +41 s. On tuned hosts it may fail: there `full` pays only its round trips and slow start (+0.7 to +1.9 s), as v1 does |
| R1 | "if pgdelta slows down more than the full copy, the protocol needs deeper pipelining before any WAN claim" | **Fires** if Δ_v1 > Δ_full at any r of 20, 60 and 120 ms, at 2 Gbit/s, **on either host setting**, in any workload (change 4). Each Δ is taken from the anchor of the same host setting (revised). When it fires, its consequence applies as registered and comes before the verdict's rows (below); A3 says where v1's excess went | **Doesn't fire on default hosts.** The closest is uniform 10% at 20 ms (+2.7 to +4.5 s against `full`'s +4.8 to +7.8). **Expected to fire on tuned hosts** at 60 or 120 ms, in hot or uniform 1%: `full` slows by +0.7 to +3.5 s there, v1 by +0.4 to +4.4 s |

**Notes written now on R1.**
- On tuned hosts R1 can fire while v1 is still about twice as fast as either copy, because a full copy's added time there is only a few round trips and slow start. It is judged as registered anyway: a rule judged only where it can't fire would favour v1 (EXP-11's lesson).
- **When it fires, its consequence comes first (revised).** "Any WAN claim" is read as written. If R1 fires at any cell, rows 1, 2 and 3 of the DR verdict carry "provisional: R1 fired" at every r, and this session makes no DR claim for v1. The rows are still computed and reported.
- The consequence is lifted only by a later test that measures deeper pipelining at every cell where R1 fired, with R1 no longer firing there. `v1_w16`'s reading at 120 ms tuned is reported beside R1, and lifts nothing.
- A3 says how much of v1's excess sits in idle waits, which pipelining can remove (A4 says whether a window of 16 is enough), and how much sits in fixed round trips, slow start and busy sending, which it can't. What no timer places is a residual, reported with them. That split is reported. It doesn't decide whether the consequence applies: the registered text has no such condition. If most of the excess can't be removed by pipelining, the write-up says so, and the consequence still stands in this experiment's results. Changing it would be an owner override under rule 8, recorded as its own ledger row.

### The DR verdict (added here)

EXP-09's bands (0.90 and 1.00), read against rule 3's baseline as EXP-11 did, at each r of 20, 60 and 120 ms, at 2 Gbit/s. 60 ms, the ledger's round trip, is the headline; 20 and 120 ms bound the claim. Hot and uniform 1% decide the rows; uniform 10% has its own rule (A8). The rows are checked in order:

| Row | Condition at r | What follows |
|---|---|---|
| **v1 not durable** (revised after the check) | A13 has reclassified v1 (`pgdelta_direct_inplace`) as not durable for the session | "v1 not durable", at every r: no claim for v1, stated as a durability failure, not as a missing cell. The other rows are still computed and reported |
| **Not judged** | a deciding cell at r (v1 or B_fast, hot or uniform 1%, default hosts) is "not measured" (A11) | stated; nothing rerun |
| **1. Holds as shipped** | `R_fast` ≤ 0.90 on default hosts, for hot and uniform 1% | v1's DR claim at r and 2 Gbit/s holds with one default TCP stream, on these VMs. D36 stands, now measured at latency |
| **2. Holds with tuned buffers** | row 1 fails; `R_fast` ≤ 0.90 on tuned hosts, for hot and uniform 1%. Not judged if a tuned deciding cell at r is "not measured": the reading goes on to row 3, flagged "tuned not measured" (revised after the check) | Raised TCP buffers bring v1 under 0.90 at r; A2 and E15 say whether one stream's window is why (revised after the check). v1 needs several streams for page data, or raised TCP buffers on both hosts. Which one is an owner decision, after the integrator question (can Omni's nodes or pods set `tcp_rmem` and `tcp_wmem`?), as EXP-11's A2 asked about `vm.dirty_*` |
| **3. Local changes only** | rows 1–2 fail or row 2 isn't judged; hot ≤ 0.90 on some host setting the design runs at r; uniform 1% ≥ 1.00 on every host setting the design runs at r. Not judged if a deciding cell of any of those settings is "not measured": a claim for v1 needs them all, so the reading goes on to row 4 (revised after the check) | v1's DR claim at r covers local changes only. Scattered changes need more streams and fewer bytes (T4) |
| **4. Out at DR latency** | hot ≥ 1.00 on every host setting measured at r (default hosts always are, or the reading is Not judged). Flagged "tuned not measured" where the tuned cell is missing (revised after the check) | Out at DR latency for local changes. A3 gives the cause (revised); no cause is stated without its timers |
| **5. Undecided** | anything else | Decided after T4 and T8. A4 alone decides D29. If A4 (i) moves the default to 16, the reading with `v1_w16` at 120 ms tuned is reported beside the row, not as the row (revised) |

- **R1 comes first (revised).** If R1 fires at any cell, rows 1, 2 and 3 at every r carry "provisional: R1 fired", and no DR claim for v1 is made from them (see the notes on R1).
- Rows 1 and 2 are verdicts for v1. As EXP-11's Confirmed did, they need a bound that passes its gate at r (A9) and isn't window-limited there (A9, revised). If it fails either, the row says "against the copies measured; bound suspect" or "window-limited bound" and is provisional.
- "The host settings the design runs at r" are default and tuned at 60 and 120 ms, and default alone at 20 ms. At 20 ms row 2 can't apply, and a 20 ms result that fails row 1 goes to rows 3–5 on default hosts alone. Row 4 reads the host settings measured at r instead, so a lost tuned cell can't turn an Out into Undecided (revised after the check).
- **A baseline lost to A13** (revised after the check) leaves every baseline, and B_fast is the fastest durable arm left. If no durable `parcopy` arm is left at r, rows 1 and 2 at r read "against the copies measured; bound not durable" and are provisional.

**Flags,** reported beside the row:
- *Near the line:* a deciding ratio within ±0.03 of 0.90 or 1.00 whose crossing would change the row. The row stands.
- *Fragile:* `R_max`, `R_trial` or `R_trig` would give another row.
- *TLS-sensitive:* at 120 ms, the row with `R_tls` (A6's reading) in place of v1's tuned ratio differs.
- *Pooled baseline unstable:* A17 (i).
- *Tuned not measured:* a tuned deciding cell at r is "not measured"; rows 2 and 3 aren't judged there, and row 4 is read on default hosts alone (revised after the check).
- *Provisional:* R1 fired, the bound is suspect, window-limited or not durable (revised after the check).

**The qualifier,** beside each row: if `R_F0` ≤ 0.90 on default hosts for hot and uniform 1% at r, the row adds "v1 is faster than one `pg_basebackup` stream, the copy Omni documents for a DR re-seed, at r and 2 Gbit/s on these VMs". At 60 ms, `probackup8` is named too if `R_rel` ≤ 0.90 against it. Where R1 fired, the qualifier is provisional too.

**At 1 Gbit/s** (uniform 1%, default hosts, 60 and 120 ms), `R_fast` and the crossover are reported. They aren't a row, since hot doesn't run there.

**Expected (E13):** at 20 ms, **row 1** (uniform 1% `R_fast` 0.68–0.87, perhaps near the line). At 60 ms, **row 2**: hot 0.33–0.57 on default hosts; uniform 1% 1.43–1.94 on default hosts and 0.57–0.80 tuned. At 120 ms, row 2 or Undecided (uniform 1% tuned 0.61–1.00). The qualifier applies at every r (`R_F0` at most 0.55 on default hosts). R1 is expected to fire on tuned hosts, so every one of these rows is expected to carry "provisional: R1 fired" (revised).

### Added predictions

| ID | Prediction | Basis |
|---|---|---|
| E1 | **The anchor reproduces EXP-11's 2 Gbit/s cell** within A10's gates: `full` 9.8–10.8 s, `parcopy8` 8.9–9.8 s, v1 hot 2.8–3.4 s and uniform 1% 4.8–5.8 s; one stream 1.88–1.96 Gbit/s. The tree at r = 0 is within 2% of EXP-11's plain tbf on the link tests and 3% on the runs (smoke item 2). **The tuned anchor** is within 0.1 s of the default anchor for v1 in each workload and for `full` (revised) | EXP-11: 10.27–10.32, 9.30–9.33, 3.11 and 5.31 s; 1.92 Gbit/s. A netem with no delay holds nothing. At 0.1 ms the bandwidth-delay product is 25 KB |
| E2 | **The emulation (revised).** Every visit passes its checks. In every run, netem's own drops (its counter less its child tbf's) are 0 and its sampled backlog stays below its limit, netem's bytes are at least 0.98 × the wire bytes, and the sender's `minrtt` is in [r, r + 2 ms]. tbf drops appear only at r > 0 | the tree's design; netem's limit is far above any window here; netem counts its child's drops as its own (kernel 6.1's `sch_netem.c`); netem orphans packets only when a delay is set |
| E3 | **Link tests,** steady state, Gbit/s. One stream, default: 1.04–1.26 at 20 ms, 0.35–0.42 at 60, 0.17–0.21 at 120, at 1 and 2 Gbit/s alike. 8 streams, default: at least 1.80 at 20 and 60 ms, 1.39–1.68 at 120 (at least 0.90 at 1 Gbit/s). 16 streams, default, at 120 ms, 2 Gbit/s: at least 1.80 (revised). One stream, tuned: at least 1.80 at 60 and 120 ms. Through the stunnel pair: within 10% of the direct test. SSH probe: 0.6–0.84 at 20 ms, 0.22–0.28 at 60, 0.11–0.14 at 120, on both host settings | 2.6–3.15 MB per round trip per default stream (half of `tcp_rmem`'s 6 MiB, less up to 15%); eight or sixteen times that; 64 MiB tuned; OpenSSH's 2 MiB channel window |
| E4 | **`full`** (pooled): 15–18 s at 20 ms; 41–52 s at 60 ms and 82–102 s at 120 ms, default; 11.0–12.2 s at 60 ms and 11.7–13.8 s at 120 ms, tuned. Its sync phase 1.2–1.45 s up to 20 ms, and 0.5–1.4 s once its stream outlasts 30 s | 2.12 GB at the stream's rate, plus the flush, 6–10 round trips and slow start; the kernel's 30 s write-back of old dirty pages |
| E5 | **`parcopy8`** (pooled): 9.4–9.9 s at 20 ms; 9.6–10.5 s at 60 ms, default and tuned; 11.5–14.5 s at 120 ms default; 10.0–11.2 s at 120 ms tuned. At 1 Gbit/s: 18.3–19.3 s at 60 ms, 18.8–20.0 s at 120 ms. **`parcopy16`** at 120 ms default: 10.1–11.0 s, faster than `parcopy8` there (revised) | link-bound until 8 default windows fall below the link at 120 ms; 16 windows don't; EXP-11's 0.47 s above link time; 3–6 round trips; slow start |
| E6 | **v1, hot,** added by latency: +0.1 to +0.7 s at 20 ms; +0.5 to +2.1 s at 60 ms and +1.1 to +4.4 s at 120 ms, default; +0.4 to +1.8 s at 60 ms and +0.9 to +3.8 s at 120 ms, tuned | derivation below |
| E7 | **v1, uniform 1%:** 6.7–8.2 s at 20 ms; 15–18.6 s at 60 ms and 27–34 s at 120 ms, default (at 1 and 2 Gbit/s alike); 6.0–7.7 s at 60 ms and 6.8–10.0 s at 120 ms, tuned | about 2.8 s of fixed work, plus 609 MB at the stream's rate, slow start and 8–18 round trips |
| E8 | **v1, uniform 10%:** 7.3–8.7 s at the anchor; 10.3–12.5 s at 20 ms; 25–31 s at 60 ms default; 8.4–10.2 s at 60 ms and 9.3–12.5 s at 120 ms, tuned | 1.13 GB at the same rates; more fixed work (more pages patched and flushed) |
| E9 | **The window,** 120 ms tuned. Hot: `v1_w1` − v1 0 to +2.0 s, `v1_w16` − v1 −0.5 to +0.1 s. Uniform 1%: `v1_w1` − v1 0 to +1.0 s, `v1_w16` − v1 −0.3 to +0.1 s. The idle part of `leaf_wait_s` is ordered w1 ≥ w4 ≥ w16 in each workload; at window 4 it is 0.2–2.3 s. The total is reported beside it (revised after the check) | 19 changed files, each at most one round trip; the window hides some behind hashing; the verifier's backlog at the end (about 0.65 s in EXP-11) absorbs some. In uniform, hashing the large files hides more |
| E10 | **The fallback,** 120 ms tuned. Uniform 1%: `v1_hc` − v1 +1.5 to +2.8 s, sending 0.45–0.55 GB more. Uniform 10%: −0.4 to +0.3 s, wire bytes within ±2%. High-churn files at least 1 in both | every node of `pgbench_accounts` differs at 1%, so whole nodes send the other 54% of its 0.94 GB (about 0.51 GB, 2.1 s at the link), less the leaf round trips saved; at 10%, 99.8% of its heap pages change anyway (EXP-01) |
| E11 | **TLS through the proxy:** `v1_tls` − v1 −0.05 to +0.15 s (hot) and −0.05 to +0.30 s (uniform) at the anchor; +0.05 to +0.40 s (hot) and +0.05 to +0.50 s (uniform) at 120 ms tuned. +0.2 to +0.8 CPU-s per VM in uniform. TLS 1.3 with `TLS_AES_256_GCM_SHA384`. Wire bytes +0.1 to +0.5% | one more round trip (0.12 s); AES-GCM with AES-NI runs at gigabytes per second per core, several times the 240 MB/s link; a localhost hop on each end |
| E12 | **`probackup8`,** 60 ms default: 28–60 s, slower than `parcopy8`. Its SSH sockets are mostly not `rwnd_limited`: the channel window, not TCP's, caps them | up to about 35 MB/s per connection; the largest file (0.94 GB) on one thread; then its 1.6 s flush. Above 60 s would mean its protocol waits a round trip per chunk or per file |
| E13 | **`R_fast` and the rows:** the table below, and the verdict's expected rows | E4–E8 |
| E14 | **Crossovers:** uniform 1% on default hosts, 1.0–1.3 Gbit/s at 60 ms ("below 1" possible) and "below 1" at 120 ms. Hot: "above 2" at every r. Tuned: "above 2" for hot and uniform 1% | E5, E7 |
| E15 | **TCP state** (`ss`): for v1 uniform and `full` on default hosts at 60 and 120 ms, `rwnd_limited` + `sndbuf_limited` is at least half of the sending socket's busy time; on tuned hosts, at most a tenth | the window model |
| E16 | **Bytes:** v1's page bytes don't depend on r, host setting or window (within 0.1%); `v1_hc` adds bytes as in E10 | pgdelta's matching doesn't see the network |
| E17 | **Durability:** every durable run passes (a) and (b) at every condition; `full_nosync` fails (a) by at least 1.5 GB in each trial | EXP-11 |

**E13, expected `R_fast`:**

| r, rate, host | hot | uniform 1% | uniform 10% |
|---|---|---|---|
| anchor, 2, default | 0.29–0.38 | 0.49–0.65 | 0.74–0.98 |
| 20 ms, 2, default | 0.31–0.43 | 0.68–0.87 | 1.04–1.33 |
| 60 ms, 2, default | 0.33–0.57 | 1.43–1.94 | 2.4–3.2 |
| 120 ms, 2, default (revised: against `parcopy16`) | 0.37–0.76 | 2.4–3.4 | – |
| 60 ms, 2, tuned | 0.32–0.54 | 0.57–0.80 | 0.80–1.06 |
| 120 ms, 2, tuned | 0.35–0.72 | 0.61–1.00 | 0.83–1.25 |
| 60 ms, 1, default | – | 0.78–1.02 | – |
| 120 ms, 1, default | – | 1.35–1.81 | – |

`R_F0` on default hosts: at most 0.27 for hot and 0.55 for uniform 1%, at 20, 60 and 120 ms. Each ratio range divides a v1 range by a baseline range end to end, so it is wider than the outcome is likely to be.

**How E3–E14 were derived** (all to be checked):
- **Inputs.** Link payload 240 MB/s at 2 Gbit/s (EXP-11 measured 1.92 Gbit/s), 120 MB/s at 1. In flight per default stream, W = 2.6–3.15 MB: half of `tcp_rmem`'s 6 MiB, less up to 15% for autotuning and the send buffer. A stream's rate is min(link, W / r): 130–157 MB/s at 20 ms, 43–53 at 60, 22–26 at 120. Tuned, W is about 64 MiB, so one stream is link-bound at every r here. Eight streams get eight windows, sixteen get sixteen.
- **Slow start** from 10 segments (14.5 KB) takes about log2(rate × r / 14.5 KB) round trips to reach a stream's cap. 0.6–1.2 times that, less one, is counted as lost per bulk transfer (CUBIC's HyStart may leave slow start early).
- **`full`:** 0.12 s to start, plus 2.12 GB at the stream's rate, plus its flush (1.33 s; 0.5–1.33 s once the stream outlasts 30 s, since the flusher writes back pages dirtied more than 30 s earlier), plus 6–10 round trips (two connections and the replication commands), plus slow start.
- **`parcopy8`, `parcopy16`:** 0.55 s, plus 2.12 GB at min(link, N W / r) for N streams, plus 3–6 round trips, plus each stream's slow start.
- **v1, uniform 1%.** EXP-11 fits it as fixed work plus link time: 5.31 − 2.54 = 2.77 s at 2 Gbit/s, and 3.89 − 1.02 = 2.87 s at 5. The sender hashes and sends from one thread, so sending that the socket buffer can't absorb adds to hashing. So: 2.6–3.0 s, plus 609 MB at the stream's rate, plus 8–18 round trips (the connect, HELLO and ACCEPT, the basis lag, exposed leaf waits, COMPLETE and the receiver's drain), plus slow start. On tuned hosts a full bottleneck queue can stretch the leaf round trips during bulk sends by up to half again.
- **v1, hot.** 29 MB on the wire, so the stream's rate barely matters; round trips do. The receiver starts hashing B when HELLO arrives, 1.5 r after the sender's connect starts; the sender starts hashing after ACCEPT, at 2 r. About 3 r are fixed. Each of the 19 changed files costs up to one more round trip unless the window hides it, and up to 0.6 s of that is absorbed by the verifier's backlog. The burst of about 25 MB when `pgbench_accounts` finishes slow-starts for about 7–11 round trips. Lower bound: 3 fixed round trips, 3 exposed leaf waits, all absorbed, half the burst's slow start overlapped. Upper bound: all 19 leaf waits exposed, nothing absorbed, the whole slow start on the critical path.
- **v1, uniform 10%:** as uniform 1%, with 1.13 GB and 2.6–4.0 s of fixed work (never measured on the VMs).
- **The fallback.** EXP-01: 45.9% of `pgbench_accounts`'s heap pages change at uniform 1%; P(a 128-page node has none) ≈ 0.54^128. So every node differs, and whole nodes add 0.54 × 0.94 GB ≈ 0.51 GB.
- **SSH:** OpenSSH's default channel window is 64 packets of 32 KiB, 2 MiB. 2 MiB / 60 ms ≈ 35 MB/s ≈ 0.28 Gbit/s.

**If the model's inputs are wrong (revised).** This derivation is code: `analysis/exp13_model.py`, built and reviewed before the smoke run. Its only inputs are W (from `tcp_rmem`'s maximum and `tcp_adv_win_scale`) and the one-stream steady rates at 20, 60 and 120 ms. A test checks that, with the inputs above, its ranges fall inside the ones written here, which were rounded outward. The smoke run records the sysctls and measures one default stream at 20, 60 and 120 ms (smoke item 7). The script then runs once on those inputs, whatever they are, and its ranges are recorded here, dated, beside the originals, before the main sweep. **Every E-range is judged as written here;** the recomputed one is reported beside it, as EXP-11 judged its E1 and E2 as written when its session was shifted. The ledger's prediction and rule, the verdict's rows and every A rule stay as written.

### Added rules

| ID | Condition | Consequence |
|---|---|---|
| A1 | The DR verdict, at each r | as its table says, after R1 (revised) |
| **A2** | **One stream (D36),** read from the verdict's rows at 60 and 120 ms | **Row 2 at either: D36 is contradicted for DR on default buffers (X).** v1 needs parallel page streams, or Omni's nodes need a TCP buffer setting: an owner decision, with the integrator question. Parallel streams go on T8's build list, sized from the 1- and 8-stream link tests, unless the owner accepts a host requirement. A row 2 counts here whether or not it is provisional: provisional marks protect claims for v1, and a contradiction of D36 isn't one. The cause must be shown (E15): if `rwnd_limited` + `sndbuf_limited` is under half of v1's busy time on default hosts there, the write-up says the tuned setting removes the loss and the timers don't show why. **Row 1 at both, neither provisional:** D36 stands, now measured at DR latency, graded B. **Any other mix** (row 1 provisional, row 1 at one r and rows 3–5 at the other, rows 3–5 at both, or Not judged at either): D36 stays at C, the readings recorded, and each row says what follows (revised) |
| **A3** | **Where v1's added time goes.** Applied whenever R1 fires, L1 fails, or a row at some r is 3, 4 or 5 | The timers split v1's Δ into four parts that add up to it (revised after the check). **Idle waits:** the idle parts of `basis_wait_s` and `leaf_wait_s`. **Fixed round trips:** `connect_s`, the idle parts of `accept_wait_s` and `complete_wait_s`, and the drain (the receiver's exit less its publication). **Sending:** `page_send_s` and the busy parts of every wait, with slow start shown by `ss`. **A residual:** Δ_v1 less the other three, reported with its sign. Each part is its median at r less its median at the anchor of the same host setting. The largest part names the fix; if the residual is the largest, the timers don't show where the time went, and no fix is named. The fix each part names: for idle waits, deeper pipelining (A4 says whether a window of 16 is enough; if not, leaf requests batched across files, or leaf digests sent unasked, go on T8's protocol list); for fixed round trips, nothing to pipeline, and the DR claim is narrowed by that much; for sending, the transport (A2's fix), not pipelining. Where R1 fired, the split is reported and doesn't lift its consequence |
| **A4** | **The window (D29),** at 120 ms tuned, hot and uniform 1%, checked in order | **(i)** `v1_w16` is at least 0.3 s faster than v1 in either workload, and at most 0.1 s slower in the other: the default window becomes 16; D29 changes, graded B. **(ii)** Otherwise, `v1_w1` is within 0.1 s of v1 in both: the window doesn't matter on this path at DR latency (the verifier's backlog hides the round trips); D29 stays 4, graded B. **(iii)** Otherwise: the window matters, and 4 is enough; D29 stays 4, graded B. **D30** (all leaf requests for a file at once) isn't varied: the idle part of `leaf_wait_s` and the request count bound what changing it could save, recorded in its row (revised). Its grade stays D |
| A5 | **The fallback (D22),** at 120 ms tuned, checked in order | **(i)** `v1_hc` saves at least 0.3 s in uniform 10% and costs at most 0.1 s in uniform 1%: it is turned on at 0.5. **(ii)** It saves at least 0.3 s in uniform 10% but costs more in uniform 1%: the node fraction can't tell 1% from 10% at this data shape (written now); a page-level trigger goes to T14, and the fallback stays off. **(iii)** Otherwise: it stays off, now measured at latency. D22's grade is set from the ledger's table |
| A6 | **TLS (O6, D37, D14).** `v1_tls` − v1 is more than max(0.3 s, 5%) at the anchor or at 120 ms tuned, in hot or uniform 1%. **Judged twice (revised):** as measured, and with `v1_tls`'s extra prefetched bytes (its median less v1's, in that cell) priced at v1's own prefetch rate there (bytes over seconds, from its statistics) and added to `v1_tls`. If the two readings differ, the one that fires governs, flagged. `R_tls` and the TLS-sensitive flag use the same reading: the slower of the two, which is the one that fires whenever they differ (revised after the check) | TLS through the proxy is on v1's critical path. The write-up says where, from the wait totals (busy plus idle), CPU, and the `ss` readings of stunnel's WAN socket (`notsent`, `unacked`). The busy and idle split isn't used for `v1_tls` (revised after the check). The verdict at 120 ms is restated with `R_tls` (the TLS-sensitive flag). Native TLS gets its own timed arm in T8 before any WAN claim with TLS. If the excess shows at the anchor, it is CPU, and the HA case pays it too. **Otherwise:** TLS is priced at the measured cost, T8 builds native TLS against that number, and O6 adds no material cost on these VMs |
| A7 | **SSH (D37).** On tuned hosts, the SSH probe carries less than half of one TCP stream at 60 and at 120 ms | SSH is dropped as a WAN transport for pgdelta: its channel window caps it whatever the host's TCP settings. D37's "SSH later" closes. Otherwise SSH stays an option, with its measured rate. The default hosts' comparison is reported beside it |
| A8 | **Uniform 10%,** at each r and host setting run | `R_fast` ≤ 0.90: v1's DR claim covers 10% scattered changes there (provisional if R1 fired). ≥ 1.00: at that r, rate and host setting, 10% scattered changes are a case for a full copy, and pgdelta's documented fallback behaviour (O2) says so. Between: reported |
| A9 | **The bound's gate,** at each condition: a `parcopy` arm's pooled median is more than (2.12 GB at the steady rate of that condition's link test with its stream count: 8, or 16 for `parcopy16`) + 1.0 s + 6 r, or its busiest stream carries more than 1.1 × the mean bytes. **A window-limited bound (revised after the check):** B_fast's arm is a `parcopy` arm whose stream count's link test at the condition is under 0.9 × the zero-delay rate at the same tbf rate (the default anchor's 8-stream test at 2 Gbit/s; step 3's probe at 1 Gbit/s) | The bound is suspect there, and its timers must explain the gap; or it is labelled "window-limited bound". At 2 Gbit/s, either way rows 1 and 2 at that r are provisional. At 1 Gbit/s, where no row is read, the label goes on that cell's `R_fast` and the crossover. If it is still B_fast, it stays there, since a slow bound only favours v1 |
| A10 | **Drift gates against EXP-11** (`exp11-cells.csv`, 2 Gbit/s), at the anchor: `full` and `parcopy8` within ±5%; v1 within ±10% (hot 3.11 s, uniform 1% 5.31 s); one stream's measured rate within ±2%; wire bytes within ±1%. Any gate fails | The session is marked shifted. Every verdict here is within the session and stands. E-ranges built on EXP-11's absolute times are judged as written and flagged |
| A11 | **Network validity.** (i) A visit fails a pre-run check | The tree and the sysctls are reset and checked once more, before any run, so no data has been seen. If they fail again, the trial stops as an infrastructure fault, recorded |
| | (ii) A run has netem's own drops above 0 (its drop counter less its child tbf's, from the same snapshot), a sampled netem backlog at its limit, netem bytes below 0.98 × its wire bytes, a failed sysctl guard, or a sender `minrtt` outside [r, r + 2 ms] (revised) | The run is excluded and reported. If more than 1 of a v1 arm's 5 runs, or more than 1 of a baseline's pooled runs, is excluded at a condition, that cell is "not measured". Nothing is rerun |
| A12 | **A tuned stream through the emulator.** At a trial's first visit to a tuned condition, one tuned stream reaches less than 0.9 × the default anchor's one-stream rate | The tuned cells there are flagged "emulator-limited": the tuned reading understates what tuning gives, against v1. Row 2 may still be declared; a row 2 that fails there is flagged |
| A13 | **Durability,** as EXP-11's A5: a durable run fails (a) or (b), or a `parcopy` run's `fsync` count is wrong | The run is excluded. More than 1 such run of an arm at a condition reclassifies the arm as not durable for the session (rule 8) and leaves it out of every baseline. Nothing is rerun. v1 reclassified so reads "v1 not durable" at every r; a baseline so lost leaves B_fast to the durable arms left (the verdict's notes) (revised after the check) |
| A14 | **Negative control:** `full_nosync` passes (a) in any trial | Check (a) is void for the session; durability rests on (b) |
| A15 | **Verification,** as EXP-11's A4, with the void rule above. A run that times out (four times its expected upper bound, at least 60 s; `probackup8` 240 s), or whose own tool aborts, is a tool failure | The arm is excluded from every baseline, as in EXP-11 |
| A16 | **`full` at 1 Gbit/s,** decided in the smoke run (item 7): one default stream measures 0.8 Gbit/s or less at 60 and at 120 ms at 1 Gbit/s | `full` doesn't run at 1 Gbit/s. Its 2 Gbit/s median stands in, labelled inferred: a window-bound stream doesn't see the link rate. Otherwise it runs there in uniform 1%, repeats 2 and 4, recorded with the new budget |
| A17 | **What the grid assumes.** (i) A baseline's per-trial medians at a condition differ by more than 3%. (ii) The tuned anchor differs from the default anchor by more than 0.1 s for v1 in a workload or for `full` (revised) | (i) The pooled reading is flagged there, and `R_trial` is stated beside every ratio there. (ii) Reported; the tuned Δs use the tuned anchor either way |

**No fix and rerun after the main sweep starts.** Fixes found in the smoke run are made before the sweep and recorded here, dated.

## Notes written before running

- **Why default hosts are the first reading.** They are what an operator gets, and what D36 names. Tuned is a host setting, like EXP-11's `full_wb`, and nobody has said whether Omni's nodes can set it. A2 compares the two. Neither reading is picked after the data.
- **Why `parcopy8` stays the bound in clear.** It is EXP-11's bound unchanged, and rule 3's baseline. Encrypting it would slow it, which would favour v1. `parcopy16` is the same bound with more streams, run only where 8 default windows can't fill the link.
- **Why pooled baselines.** See [Repeats and order](#repeats-and-order). `R_trial` shows whether a trial effect would move a row.
- **Uniform 10% is near the line even at 0.1 ms** (`R_fast` 0.74–0.98 expected). Its rows are about bytes as much as latency.
- **Expected outcome, written now (revised):** L1 holds for hot and fails for uniform. L2 holds on default hosts. R1 doesn't fire on default hosts and probably fires on tuned hosts; then the registered consequence applies, every row for v1 is provisional, and this session makes no DR claim for v1. The rows as computed: 20 ms row 1, 60 ms row 2, 120 ms row 2 or Undecided. If the smoke run projects the budget's middle, the cuts drop hot at 20 ms, and the 20 ms verdict is then Not judged (revised after the check). A2 contradicts D36 for DR on default buffers. A3 puts most of v1's tuned excess in fixed round trips, slow start and busy sending rather than in idle waits, so pipelining alone may not lift R1's consequence. A4 lands near its 0.3 s line. A5 keeps the fallback off. A6 prices TLS under its line. A7 drops SSH.
- **netem and TCP Small Queues.** Delayed packets held in the sender's own qdisc could count against TCP Small Queues and cap one stream. netem orphans its packets when a delay is set, which should prevent that. Smoke item 6 checks it. If one tuned stream can't reach 0.9 of the link at 120 ms, the delay moves to the receivers' ingress (an IFB device) before the main sweep, recorded here.
- **Session drift.** EXP-11's session ran 14–18% slower than EXP-09's in v1. Nothing here is compared with EXP-11 except through A10.
- **The trials run in a fixed order,** so workload and time in the session are confounded, as in EXP-04b and EXP-11.
- **"VM hours"** means the pair's wall-clock time, as EXP-08 to EXP-11 read it.

**What EXP-11 taught, applied here:**
- **Counts come from the tools' own counters** (netem, tbf, `nstat`, pgdelta's), never from a list the analysis truncated. A counter's meaning is checked against its source: netem's drops include its child's.
- **No cause without a timer.** Window limits must be shown by `ss`, pgdelta's round trips by the idle parts of its waits (in clear; not through the proxy). No row states a cause.
- **Smoke checks read the records, not the log.** EXP-11's smoke item 12 said the reference digests matched; the trial records said they didn't. Every smoke item below names the record it reads.
- **Untimed steps can change what is measured later.** No column is measured after verification (`usable_s` is dropped), and the cache drop follows every set-up step.
- **Predicates name their sets.** Every rule here names its arms, conditions and workloads.
- **The arm under test is fixed in advance.** No side arm takes v1's place in a row.
- **A rule must be able to fire.** EXP-11's void rule made a leg of its A4 impossible; A15 fixes it. R1 is judged where it can fire (change 4), and the emulator check no longer excludes the runs R1 needs.
- **Every expected range shows its arithmetic,** its inputs are checked before the sweep, and the ranges judged are the ones written here.

## Changes from the ledger (rule 8)

Each becomes a ledger row with its own grade when this is registered.

1. **"pgdelta" is v1's path** (`pgdelta_direct_inplace`, window 4), not staged v0. v0 is graded X (D10p), O9 dropped staged-only work, and EXP-11 made v1's path the reference. The window is the sender's setting in both. Grade B.
2. **The full-copy side** keeps one `pg_basebackup` stream as the registered "full copy", and adds `parcopy8` (the ledger's October 24 note, rule 3), `parcopy16` where 8 default windows can't fill the link, and `probackup8` at 60 ms (the only released parallel copier found). Grade B.
3. **TCP buffers are a factor:** Debian's defaults, and maxima raised on both hosts. The registered prediction is judged on the defaults, which is what ships and what D36 names; the tuned reading is reported beside it, with its own anchor and its own rules. Grade B.
4. **R1 is judged at 20, 60 and 120 ms, on both host settings, and fires on either.** The ledger names neither. On default hosts a full copy is crushed by its window, so judging only there would leave the rule little chance to fire, in v1's favour. When it fires, its consequence applies to every WAN claim from this session, before the verdict's rows. Grade C: a value judgment.
5. **"SSH" becomes an SSH channel probe** at each round trip on both host settings, plus `pg_probackup`'s own transport at 60 ms. No pgdelta-over-SSH arm: under O6 TLS is pgdelta's transport, and the probe decides the transport question. Grade B.
6. **TLS is measured through a proxy,** not built into pgdelta. v1's main arm and the baselines stay in clear. Grade B.
7. **Windows 1 and 16, the fallback, and TLS at latency run at one cell** (120 ms, 2 Gbit/s, tuned), their best case. Window 4 runs everywhere. Grade B.
8. **"10% changes" is uniform 10%,** the realistic DR divergence and the fallback's case. Append and hot 10% are left out. Grade B.
9. **Link rates:** 2 Gbit/s everywhere, and 1 Gbit/s for uniform 1% on default hosts at 60 and 120 ms, to bracket its crossover. Grade B.
10. **The harness's control path is outside the emulated WAN.** The trigger a real deployment pays is reported as `R_trig`. Grade B.
11. **Added:** the anchor and the tuned anchor in this session; `parcopy16` at 120 ms default; the emulator's tree and its per-visit and per-run checks; the TCP metrics flush; the `ss` sampler, pgdelta's protocol timers and their busy and idle parts; the DR verdict and A2–A17; the negative control; the void rule restricted to user-table files.
12. **Cost.** The ledger budgeted half a day and about 2 VM hours. This needs about 4.3 days of harness work and about 3.8 hours of the pair (3.5–4.3; below). The lead hasn't accepted the overrun yet. The cut line below is 3.5 hours, as in the draft; at the projection's middle it cuts hot at 20 ms, uniform 10% to 3 repeats, and `probackup8`. A line of about 3.8 hours would run the whole design. That is a separate request to the lead, not part of this design (revised after the check).

## Revisions before running (October 24, 2026)

An independent reviewer checked the merged draft before anything was built or run, against the ledger, EXP-09, EXP-11 (with its revisions and corrections), the harness code (`run_exp04.py`, `agent.py`, `pgdelta/sender.py`, `receiver.py`, `transport.py`), EXP-11's raw records and kernel 6.1's netem source. It made 12 findings, 4 major. The project lead decided each. Each was checked against the draft, the ledger, EXP-11 and the code before it was applied. Where a fix offered options, the option taken is the one that doesn't favour v1, and the reason is given. The ledger's registered text is unchanged. Below, "finding n" is the review's nth finding. Changes, with the finding each answers:

**The emulator**
- **netem's drops are read net of its child's** (finding 1, major). In kernel 6.1, when netem hands a due packet to its child tbf and the tbf refuses it, netem counts the drop as its own, and the `prio` root counts it too. The draft expects tbf drops in every tuned bulk run, and for default `parcopy8` at 20 ms and at 1 Gbit/s and 60 ms. Its rule "netem drops nothing" would have excluded all of those runs, and left row 2, R1 on tuned hosts, A4–A6 and the 20 ms verdict unjudgeable. Now the emulator overflows only if netem's drops less tbf's, from the same `tc -s -j` snapshot, grow, or netem's backlog, sampled every 0.5 s, reaches its limit. The emulated WAN's notes, the per-run checks, E2, A11 (ii) and smoke item 5 say so. The parser gets a unit test on real `tc -s -j` output in which tbf's drops show in netem's counter. The setup now says that drops at the shaper appear only from r > 0: at the anchor netem has no delay, doesn't orphan packets, and leaves TCP Small Queues in force, which is why EXP-11's tbf saw none and the anchor can't reveal the problem.

**The verdict and the rules**
- **R1 comes first** (finding 2, major). When R1 fired, its consequence ("no WAN claim from window-4 cells") contradicted rows 1 and 2, and the draft left the conflict to the owner after the data. Now, if R1 fires at any cell, rows 1, 2 and 3 carry "provisional: R1 fired" at every r, and this session makes no DR claim for v1. The consequence is lifted only by a later test that measures deeper pipelining wherever R1 fired. "What would change the plan", the notes on R1, A1, A8, the qualifier and the expected outcome follow. Two options in the review's fix are not taken, both because they would favour v1:
  - **The pre-registered split.** The review offered: the consequence applies if A3 puts at least half of v1's excess in protocol waits, and is recorded as not applying if less. The registered text has no such condition, and the split would let the rule stop applying in v1's favour. So the consequence applies whenever R1 fires. A3's split is reported beside it and lifts nothing; an override would be the owner's, under rule 8.
  - **The scope.** The review's example made rows provisional only "at any r and host setting where R1 fired". The registered text says "before any WAN claim", so every row for v1 is provisional, at every r.
- **Row 5 no longer re-picks v1's arm** (finding 3, major). Its parenthesis ("if a window of 16 brings a deciding ratio to 0.90 or below, the default becomes 16 and the row is restated with it") was a second, looser way to change D29 than A4, and chose v1's arm on the data it is judged on. EXP-11's check of its revision reverted the same move. It is deleted. Undecided stays Undecided, and A4 alone decides D29. If A4 (i) moves the default to 16, the reading with `v1_w16` at 120 ms tuned is reported beside the row, not as the row. "v1's arm is fixed in advance" now says that no side arm takes v1's place.
- **Rows when a tuned cell is missing, and A2's mixes** (finding 10). Row 2 is not judged where a tuned deciding cell isn't measured; the reading goes to row 5, flagged "tuned not measured", with the default hosts' row beside it. Rows 3 and 4 read "every host setting the design runs at r" (default alone at 20 ms). A2 now covers every mix. A row 2 at either r contradicts D36, provisional or not, since provisional marks protect claims for v1. "D36 stands" (B) needs row 1 at both, neither provisional. Any other mix, Not judged at either included, leaves D36 at C. That is the review's "D36's grade follows the worse of the two rows". (Rows 3 and 4 changed after the check: see below.)
- **Row 4 states no cause** (finding 11). "pgdelta's own round trips dominate" was a cause no timer had shown; hot at 1.00 or more could also come from slow start after idle or the receiver's drain. Row 4 now reads "Out at DR latency for local changes. A3 gives the cause".
- **A window-limited bound** (finding 12). At 120 ms, 2 Gbit/s on default hosts, eight default windows carry 1.39–1.68 Gbit/s, less than the link, so `parcopy8` is window-bound there, and A9's gate, built from the same 8-stream rate, can't see it. A slower B_fast favours v1 in row 1 there. The review offered two fixes: flag B_fast as a window-limited bound and make row 1 provisional, or run `parcopy` with 16 streams at that cell. Flagging alone would leave the slower bound in B_fast, in v1's favour, so `parcopy16` now runs there: repeats 2 and 4 of uniform 1% and hot, 4 runs, pooled, as a B_fast candidate, with a 16-stream link test at each trial's first visit. A9 gates it with its own stream count. The flag is kept as well, for any cell where B_fast's arm is a `parcopy` whose stream count's link test is under 0.9 of the anchor's 8-stream rate: it is labelled "window-limited bound" and rows 1–2 are provisional there. E3, E5 and E13's 120 ms default row are recomputed with the draft's own model: `parcopy16` 10.1–11.0 s; `R_fast` hot 0.37–0.76 (was 0.28–0.67) and uniform 1% 2.4–3.4 (was 1.9–3.0). The expected row there doesn't change. (The flag's comparison changed after the check: see below.)

**The timers**
- **Waits are split into busy and idle** (finding 4, major). Leaf requests travel in the same TCP stream as page data. In the sender's loop, `_plan(i+1)` hashes the next file and writes its leaf requests after `_finish(i − window)` has written that file's pages. On tuned hosts `tcp_wmem`'s 128 MiB maximum lets the send buffer autotune to tens of MB, so after `pgbench_accounts`' pages `send()` returns with much of them unsent. The next leaf request waits behind them, and behind a bottleneck queue of up to r, while the link is fully busy. That time landed in `leaf_wait_s`; on default hosts the 4 MiB buffer keeps it in `page_send_s`. So A3's split depended on the buffer size, and on tuned hosts it would have named pipelining for link-bound sending. Now the sender reads its socket's unsent bytes (`SIOCOUTQNSD`) and unacknowledged bytes (`SIOCOUTQ`) every 1 ms during each wait. A slice is busy when bytes are unsent, or when more are in flight than the path holds without queueing (the visit's one-stream rate times its ping minimum, passed by the harness); otherwise it is idle. A3 counts only idle parts as protocol waits, and busy parts as sending. A test on a slowed local TCP connection, and smoke item 13's check that the two parts add up to each wait, are added. The review's other way, estimating busy time from queued bytes over the visit's stream rate, isn't taken: polling measures it, and no timer is replaced by a model. (A3's parts and the split's use for `v1_tls` changed after the check: see below.)

**Anchors**
- **A tuned anchor runs in the sweep** (finding 6). On tuned hosts R1 compares Δ_full (expected +0.7 to +3.5 s) with Δ_v1, both from the default anchor. Smoke item 11 checked the tuned anchor only for v1 uniform and `parcopy8`, 2 runs each, at 3%, and never for `full`, the arm R1 compares against; 3% of `full` is 0.31 s, a large share of its tuned Δ. The review offered two fixes: add `full` and v1 hot to the smoke check with a 0.1 s tolerance, or run a tuned anchor in the sweep. The smoke check would decide, after seeing 2 runs, whether the tuned Δs get their own anchor, and 2 runs can't resolve 0.1 s; so the tuned anchor runs in the sweep. v1 runs there in all 5 repeats and `full` in repeats 2 and 4 (6 runs, pooled), in every trial: 21 runs. L1's and L2's tuned readings and R1 on tuned hosts use it. The Δs from the default anchor are reported beside them. A17 (ii) now reports the gap between the anchors; smoke item 11 checks the tuned anchor's guard.

**TLS**
- **stunnel keeps its default close timeout** (finding 5). With `TIMEOUTclose = 0`, dest's stunnel sent close_notify and closed both sockets as soon as the receiver half-closed, so the receiver's drain ended at once and `v1_tls`'s run ended about r/2 earlier than v1's (60 ms at 120 ms). With the default, dest's stunnel waits for the source's close_notify, which follows the sender's close, so the drain mirrors the clear run. Smoke item 12 checks, from the receiver's statistics and the run record, that publication to exit is about one r in both arms. Separately, stunnel in server mode connects to the receiver only after its TCP and TLS handshakes, so `v1_tls`'s receiver prefetches B for longer than v1's, which native TLS wouldn't give. The review counted 1.5 r; this revision counts about 1 r (clear's receiver accepts when the handshake's last ACK arrives, about 1.5 r after the connect starts; stunnel connects after the client's Finished, about 2.5 r). The pricing uses measured bytes, so the count doesn't matter. Both arms' prefetched bytes are recorded, and A6 is judged twice: as measured, and with the extra bytes priced at v1's own prefetch rate in that cell. The review asked for the second reading as a flag; that would leave the choice between the two to after the data. So, if they differ, the reading that fires governs, flagged, which doesn't favour v1.

**Durability**
- **Idle noise over long windows** (finding 7). Check (a)'s noise came from 20 windows of 5 s, copied from EXP-11, whose runs lasted 2–13 s. Here `full` runs 41–102 s at 60–120 ms on default hosts, and background dirtying on dest can build up until the 30 s expiry, so a durable run could fail (a) spuriously and leave a cell "not measured". Smoke item 18 adds 3 windows of 120 s, and `NOISE` is the largest increase over any window. If `NOISE` exceeds 14 MiB, check (a) can no longer catch a hot v1 that skipped its fsyncs (about 29 MB written), and the write-up says so. This adds about 6 minutes to the smoke run. (One `NOISE` for every run changed after the check: see below.)

**Harness and analysis**
- **The link test's warmup and per-second rates are built** (finding 8). `run_exp04.py` has `--link-test-seconds` (default 3) but no `--link-test-warmup`, and `agent.py`'s `blast-recv` computes one steady rate after `--warmup` (default 0.5 s, which `run_exp04.py` never sets) and keeps no per-second rates. So the run lines would have been refused, and smoke item 8 and the per-visit steady rate couldn't be computed as written. The build table now has `--link-test-warmup` passed through as `blast-recv --warmup`, per-second byte counts for one stream and for several, and the median-of-last-3 steady rate, with tests. The review's other option, `blast-recv`'s existing steady rate after a 3 s warmup, isn't taken, because smoke item 8 needs the per-second rates.
- **The model is a script, and the original ranges are judged** (finding 9). The recomputed E-ranges would have been written after a smoke run that includes a scale-70 pass of every arm at every uniform 1% condition, and the draft didn't say which ranges were judged. Now `analysis/exp13_model.py` is the derivation as code, built before the smoke run; its only inputs are the sysctls and the one-stream steady rates, and a test checks that its ranges fall inside the ones written here. Every E-range is judged as written; the recomputed one is reported beside it.
- **Every smoke item names its record** (finding 11). The draft said so, but its smoke table had no such column, and items 2–8, 10–12, 16 and 17 named none: EXP-11's smoke item 12 was wrong for that reason. The smoke table now has a Record column. (The fields it names are built after the check: see below.)

**Budget**
- **The budget is recomputed** (follows from findings 6, 7 and 12). 254 runs in the sweep instead of 229, and about 80 in the smoke run instead of 75. About 143 minutes of sweep instead of 132, and 46 of smoke instead of 40. About 3.7 hours of the pair (3.3–4.2) instead of 3.4, and about 4.2 days of harness work instead of 3.5 (the busy and idle split, the per-second link rates, the model script, the netem parser and sampler, `parcopy16` and the tuned anchor). The proposed cut line moves from 3.5 to 4.0 hours, and `v1_tls` at 120 ms tuned moves from the first cut to the last. At 3.5 hours the new projection would have cut it first, and it is A6's only leg where TLS's extra round trip shows, so cutting it would favour v1. The tuned anchor and `parcopy16` join "never cut". The lead hasn't accepted the overrun. (The cut line and `v1_tls`'s place changed after the check: see below.)

**Rejected findings:** none. All 12 held when checked against the draft, the ledger, EXP-11 and the code. The parts of their fixes not taken, each because it would favour v1 or replace a measurement with a model, are named above: finding 2's split and its per-cell scope, finding 4's estimate, finding 5's flag-only second reading, finding 6's smoke check, finding 8's existing steady rate, and finding 12's flag alone. One number in a finding is corrected: finding 5's extra prefetch is about 1 r, not 1.5 r.

**Found while checking numbers**
- The draft's model reproduces its budget (229 runs; 62, 37 and 33 minutes for the three invocations), and its E-ranges once rounded outward: the written ranges are up to 0.02 wider in the ratios, and are kept as written.
- `run_exp04.py`'s `--link-test-seconds` defaults to 3 s; the run lines pass 6.
- The receiver already records its prefetch's bytes and seconds; A6's pricing needs no new field in pgdelta.

### After a check of the revision

A second reviewer checked this revision against the review's findings, the ledger, EXP-11 and the harness code (`run_exp04.py`, `measure.py`, `agent.py`, `pgdelta/sender.py`). It made 13 findings, none major. It also named the review's findings 4, 10, 11 and 12 as only partly applied; each of those is one of its findings below. Each was checked against this document, the ledger, EXP-11 and the code before it was applied. Where a fix offered options, the option taken is the one that doesn't favour v1, and the reason is given. The ledger's registered text is unchanged. Below, "check n" is the check's nth finding. Changes, with the check each answers:

**The timers**
- **The busy parts of the accept and complete waits go to sending** (check 1; the review's finding 4, partly applied). A3 counted all of `accept_wait_s` and `complete_wait_s` as fixed round trips, while sending took the busy parts of every wait. So their busy parts were counted twice, and the parts didn't add up to Δ_v1. Both busy parts are real. HELLO carries the file inventory (tens of KB, `pgdelta/sender.py`), which slow-starts inside the accept wait. COMPLETE is written after the last pages, so on tuned hosts its wait holds unsent pages. Now fixed round trips are the connect, the drain and the idle parts of those two waits, and their busy parts are sending. Idle waits, fixed round trips, sending and a residual add up to Δ_v1; the residual is reported, and if it is the largest part, no fix is named. The sender gains a `connect_s` timer, since A3 counts the connect and no timer had it. E9 is judged on `leaf_wait_s`'s idle part, with the total beside it.
- **The split isn't read for `v1_tls`** (check 2). There the sender's socket is the localhost hop to stunnel. Bytes waiting in stunnel's buffers and its WAN socket don't show in it, so a wait can read idle while the link is busy. A6 now says where TLS costs from the wait totals, CPU, and the `ss` readings of stunnel's WAN socket (`notsent` and `unacked`, added to the sampler). The split is recorded for `v1_tls` and never cited. One detail of the check is corrected: the sender's queue doesn't always read near 0, since its bytes show as unsent once stunnel stops reading. The conclusion holds: it never sees what waits beyond stunnel.

**Durability**
- **Check (a)'s limit follows the run's length** (check 3). One `NOISE`, the largest increase over 5 s and 120 s windows, applied to every run. So the 120 s noise needed for `full`'s 41–102 s runs also loosened check (a) for v1's 3–34 s runs, and in the sweep (a) is v1's only durability check. That favoured v1. Now each run's limit is max(4 MiB, the noise of the shortest window length that covers its clock), where a length's noise is the largest increase over windows of that length or shorter. Smoke item 18 adds 6 windows of 30 s (about 3 minutes), and the run lines pass a table. The check's wording, "windows at least as long as its clock", isn't taken word for word: for a 3 s run every window is that long, so it would again apply the 120 s noise.

**The verdict and the rules**
- **The window-limited-bound flag compares like with like** (check 4; the review's finding 12, partly applied). It compared a condition's link test with the anchor's 8-stream rate, about 1.92 Gbit/s. At 1 Gbit/s, 8 streams can't pass about 0.96, so the flag fired there by construction. Now it compares with 0.9 × the zero-delay rate at the same tbf rate: the default anchor's 8-stream test at 2 Gbit/s, step 3's probe at 1 Gbit/s. At 2 Gbit/s it makes rows 1 and 2 at r provisional, as before. At 1 Gbit/s, where no row is read, it labels that cell's `R_fast` and crossover. The check's other option, the flag at 2 Gbit/s only, isn't taken: it would leave a window-limited bound at 1 Gbit/s unlabelled, in v1's favour. "The anchor" alone now means the default anchor, and A9, A12, the crossovers' x and `R_adj` say "default".
- **Row 4 reads the host settings measured at r** (check 5; the review's finding 10, partly applied). The revision read rows 3 and 4 over "the host settings the design runs at r", sent a lost tuned cell straight to row 5, and didn't note the departure. So a hot ratio of 1.00 or more on default hosts read Undecided instead of Out whenever a tuned cell was lost, in v1's favour. Now a lost tuned cell leaves row 2 not judged, and the reading goes on to rows 3 and 4. Row 4 reads every host setting measured at r, flagged "tuned not measured". Row 3 is a claim for v1, so it still needs every setting the design runs at r, and otherwise goes on to row 4. That is the review's reading for row 4, and a stricter one than the review's for row 3, for that reason.
- **A failed durability check is a reading, not a gap** (check 6). The revision put A13 in the Not judged row without a note, so a v1 that failed durability would read "Not judged", softer than a failure. Now, if A13 reclassifies v1 as not durable for the session, every r reads "v1 not durable": no claim for v1, stated as a durability failure. A baseline reclassified by A13 leaves every baseline, and B_fast is the fastest durable arm left. With no durable `parcopy` arm left at r, rows 1 and 2 there read "bound not durable" and are provisional. Not judged keeps A11 alone.
- **Row 2 states no cause** (check 7; the review's finding 11, partly applied). "One default stream is window-limited at r" was a cause no timer had shown when the row is read. Row 2 now reads "Raised TCP buffers bring v1 under 0.90 at r; A2 and E15 say whether one stream's window is why". For the same reason, the plan table's third row now says "Default TCP buffers cost the DR case", with `ss` saying why.
- **The plan table matches A2 and R1** (check 10). Its second row said "D36 stands" whenever v1 beats the bound on default hosts. A2 lets D36 stand only with row 1 at 60 and 120 ms, neither provisional, and R1 is expected to make them provisional. The row now says so. Its first row's "at any cell" is now "at any r of 20, 60 and 120 ms at 2 Gbit/s", where R1 is judged.
- **`R_tls` and the TLS-sensitive flag use A6's reading** (check 12). Nothing said whether they use the measured `v1_tls` or the priced one. Now they use the slower of the two, which is the one that fires whenever the two differ. The check said "whichever reading governs A6", which leaves a choice when the two agree. The slower one is taken there too: the measured one keeps the proxy's longer prefetch, which native TLS wouldn't give, so it would favour v1.

**Records and the harness**
- **Every Record path is built** (check 8; the review's finding 11, partly applied). In the code, `blast-send` times no connect, and `run_exp04.tool_reported()` copies selected keys from pgdelta's statistics and keeps the prefetch at `tool_reported.prefetch`. The build table now adds each link-test stream's `connect_s`; `run.json`'s `control` and `teardown`; the run record's `tcp_metrics` and `processes`; the trial record's `visits` and `first_visit`; and `tool_reported()` carrying `window`, `high_churn_node_fraction`, `high_churn_files`, `leaf_requests`, the sender's timers with their parts, and the receiver's `hello_wait_s`, `recv_wait_s` and `publish_s` under `tool_reported.receiver`. Smoke item 12 now reads `tool_reported.prefetch.bytes`. `analysis/exp13.py` checks that every Record path in the smoke table exists in the smoke run's records, before the sweep.
- **EXP-11's per-trial digests and probes run in every trial** (check 9). `run_exp04.py` has no `--reference-digests`, so the uniform 1% run line would have been refused. Also, any run that selects one of EXP-11's arms (`measure.T2_METHODS`, which holds `parcopy8`) takes the start and end reference digests and EXP-11's probes in every trial. So all three invocations would have run them, unbudgeted, against "There are no floor probes". The check offered two fixes: keep them and budget them, or build a switch for the digests and one to skip the probes. Neither favours v1: both are untimed and decide nothing but A15's void rule. The first is taken, because each trial's steps then stay EXP-11's, which A10's drift gates compare against, and nothing new is built. The option is gone from the run line. Steps 2 and 6 and the budget say so (about 1.5 minutes per trial).
- **The model runs on the smoke inputs every time** (check 11). The model section said so, but smoke item 7 ran it only when a rate was more than 25% off. Item 7 now runs it unconditionally. A rate off by more than 25% flags the E-ranges built on it, which are still judged as written.

**Budget**
- **`v1_tls` at 120 ms tuned is never cut, and the cut line is back at 3.5 hours** (check 13). The revision raised the line to 4.0 hours because at 3.5 hours the first cut would have dropped `v1_tls`. Once that cut moved last, the reason no longer held, and the extra half hour was a budget increase no finding asked for. Cutting `v1_tls` there favours v1, so it now can't be cut at all. The check's other option, asking the lead for 4.0 hours, isn't part of this design: the budget is the lead's call. At the new projection's middle (about 3.8 hours) the 3.5-hour line applies cuts 1 to 3: hot at 20 ms, which leaves the 20 ms verdict Not judged; uniform 10% at 3 repeats; and `probackup8`. A line of about 3.8 hours would run the whole design; that goes to the lead as a separate request.
- **The budget is recomputed** (follows from checks 3 and 9). About 149 minutes of sweep instead of 143, and 49 of smoke instead of 46. About 3.8 hours of the pair (3.5–4.3) instead of 3.7, and about 4.3 days of harness work instead of 4.2 (the record fields, the noise table, `connect_s`). The run count doesn't change.

**Rejected findings:** none. All 13 held against this document, the ledger, EXP-11 and the code. Two are applied in a corrected form, said above: check 3's window rule, which as worded would apply the 120 s noise to every run, and check 2's claim that the sender's queue reads near 0. Each of the check's "unaddressed" items is one of checks 1, 2, 4, 5, 7 and 8, and is answered there.

## Where the two drafts differed

| Question | Realism draft | Measurement draft | Kept, and why |
|---|---|---|---|
| TLS | through stunnel | built into pgdelta (Python `ssl`) | **stunnel.** The objection that a proxy's window is measured instead of pgdelta's holds for an SSH tunnel's channel window, not for stunnel, whose WAN socket autotunes like pgdelta's (smoke item 12 checks it). Native TLS on pgdelta's two-thread connection is T8's |
| v1's main arm | through TLS | in clear | **In clear:** today's protocol for the registered prediction, and like for like with the clear baselines. TLS priced at two conditions, `R_tls` beside |
| Link rates | 1 and 2 Gbit/s | 2 Gbit/s | **2 everywhere, 1 only where it brackets a crossover** (uniform 1%, default, 60 and 120 ms) |
| Control path | port 22 undelayed | harness on port 2222, everything else to the peer delayed | **Port 2222,** so `pg_probackup` and the SSH probe cross the delayed path |
| Bottleneck queue | 50 ms | max(50 ms, r) | **max(50 ms, r):** it doesn't penalise one stream for the queue |
| R1 | both host settings | default hosts | **Both**, so the rule can fire |
| Baselines | pooled over trials, 2 per trial | per trial | **Pooled**, with `R_trial` and A17 beside |
| `pg_probackup`; SSH | dropped; a probe | a 3-run diagnostic; a v1-over-tunnel arm | **3 runs at 60 ms, and the probe;** no tunnel arm |
| Side arms | one cell | several cells | **One cell,** their best case |
| Emulator control | the anchor against EXP-11 | a plain-tbf cell in hot | **The anchor, plus a plain-tbf check in the smoke run** |
| From the measurement draft | | | the TCP metrics flush, the `ss` sampler, the netem byte and `minrtt` checks, the void rule, `usable_s` dropped, `R_trig`, recomputing the model before the sweep, the negative control, the IFB fallback |
| From the realism draft | | | the uniform 10% rule, the "what would change the plan" table, `full` at 1 Gbit/s inferred (A16), the SSH probe on both host settings, A3's attribution of v1's added time |

## Limitations, known before running

- **netem isn't a WAN.** Constant delay; no jitter, loss or reordering; one bottleneck queue; CUBIC only. Real cross-region paths would hurt one loss-based stream more than 8. **This favours v1 and `full` over the `parcopy` arms.**
- **One shaper caps the aggregate.** Real cloud paths may also cap each flow (3 Gbit/s on external IPs), above the rates run here.
- **Two link rates,** and 1 Gbit/s only for uniform 1% on default hosts. Crossovers above 2 Gbit/s aren't measured.
- **The tuned setting is one value** (128 MiB maxima on both hosts), and only the maxima change. Slow start after idle stays on. GKE's node defaults may differ from Debian's.
- **The control path is outside the WAN.** A real deployment must trigger its sender across it; `R_trig` prices one round trip.
- **TLS through a proxy,** with a localhost hop on each end, and a longer prefetch that A6 prices from measured bytes; native TLS's cost is T8's. The baselines send in clear. Both favour the baselines.
- **pgdelta's busy and idle waits** come from 1 ms polling of the socket against a threshold from the visit's link test and ping. Each part is good to a few ms per wait, not exact. They don't hold for `v1_tls`, whose sender's socket is the localhost hop to stunnel (revised after the check).
- **SSH:** OpenSSH's defaults with `aes128-gcm`. Larger channel windows (HPN-style patches) aren't tested.
- **Stopped sources** for v1's arms and the `parcopy` arms, since v1's path can only read a stopped source today (backup mode on a live source is T8's). `full` and `probackup8` read a running, idle one. `R_adj` uses `full`'s adder at the anchor only.
- **An idle source:** no slowdown is measured (T10a).
- **2.1 GB fits in RAM; local NVMe; ext4; one VM pair.** v1's verification re-read comes from the page cache. No ratio is projected to TB scale (rule 10), though a window-bound stream's cost grows with its bytes.
- **pgbench-shaped changes,** hot and uniform at 1%, uniform at 10%; append is assumed to behave as hot. No TPROC-C (T4).
- **One fallback threshold.** At this data shape any node fraction below 1 fires on `pgbench_accounts` in both uniform trials.
- **The window, the fallback and TLS at latency are measured at one cell,** their best case.
- **Baselines pooled over trials,** 2 runs per trial; `parcopy16` 4 runs at one cell; `probackup8` 3 runs in one trial.
- **The trials run in a fixed order,** so workload and time in the session are confounded.
- **One session.** Every ratio is within it.

## Smoke run

Before the main sweep, on the two new VMs: the scale-5 items in uniform 1% and hot (every arm at the anchor, 60 ms default and 120 ms tuned, and v1 and `full` at the tuned anchor, 1 repeat), then the scale-70 items. Each item says what it settles, the record it reads, and what happens if it fails. Results are recorded here, dated.

Records: `run.json` (the invocation: environment, sysctls, versions, `dirty_idle_noise`, `syscall_audit`); the trial record `trials/<trial>.json` (`visits`: the parsed tree and band counters, pings, control no-ops, link tests with per-second rates; `first_visit` probes; `reference_digests`); the run record `runs/<trial>/r<repeat>-<condition>-<method>.json` (`condition`, `sysctl`, `qdisc` snapshots and samples, `ss`, `nstat`, `tcp_metrics`, `tool_reported` with pgdelta's statistics, `phases`, `durability`, `verification`, `method_flags`); stunnel's logs under `logs/`. The fields named here that EXP-11's records lack are built (below), and `analysis/exp13.py` checks that every Record path in this table exists in the smoke run's records before the sweep (revised after the check).

| # | Item | Record | Pass | If it fails |
|---|---|---|---|---|
| 1 | **The tree** (`wan.sh` at r/2 = 0, 10, 30 and 60 ms; `off`): `tc -s` parses to the intended tree; the band counters show a control no-op on port 2222, the Mac's SSH and a metadata request in the plain band, and pings, a port-22 SSH and a data-port connection in the delayed band | trial record: `visits[].tree`, band counters before and after each probe | all as intended | fix `wan.sh` |
| 2 | **Emulator control,** scale 70: the tree at r = 0 against EXP-11's `throttle.sh` at 2 Gbit/s: one- and 8-stream link tests, and 2 runs each of `parcopy8` and v1 uniform | trial record: `visits[].link_tests[].steady_gbit`; run records: `totals.total_s` | within 2% (link) and 3% (runs) | find the cause before the sweep; if none is found, reported, and every Δ stays within the tree |
| 3 | **Round trips** at each r: ping minimum and median; the link test's TCP connect time | trial record: `visits[].pings`, `visits[].link_tests[].connect_s` | [r, r + 1 ms] and [r, r + 2 ms] | fix the tree |
| 4 | **The control path** on port 2222: the multiplexed connection, agent calls and sender launches all use it | `run.json`: `control` (port, multiplexing); trial record: `visits[].control_noops` | no-op within 5 ms of the anchor's at 120 ms | fix the harness |
| 5 | **netem's own drops** (revised): an 8-stream tuned link test at 120 ms | trial record: `first_visit.link_tests[].qdisc` (netem and tbf drop counters from one snapshot; netem's sampled backlog) | netem's drops less tbf's: 0; netem's largest backlog below its limit; tbf's drops also show in netem's counter, as the parser assumes | raise netem's limit if its own drops are above 0; fix the parser if tbf's drops don't show in netem's counter |
| 6 | **One tuned stream at 120 ms** (TCP Small Queues, the 64 MiB window) | trial record: `first_visit.link_tests[]` (one stream and 8, `steady_gbit`) | at least 0.9 of the 8-stream rate | delay at the receivers' ingress (IFB); recheck items 1–6 |
| 7 | **The model's inputs:** both VMs' sysctls; one default stream at 20, 60 and 120 ms at 2 Gbit/s, and at 60 and 120 ms at 1 Gbit/s. In every case `analysis/exp13_model.py` then runs on the measured inputs, and its ranges are recorded here, dated, beside the originals, which are the ones judged (revised after the check). The 1 Gbit/s readings decide A16 | `run.json`: `environment.sysctl`; trial record: `visits[].link_tests[].steady_gbit` | within 25% of 1.26, 0.42 and 0.21 Gbit/s | the E-ranges built on that rate are still judged as written, flagged "input off by more than 25%" |
| 8 | **Link-test length:** per-second rates at 120 ms tuned are steady over the last 3 s of 6 | trial record: `visits[].link_tests[].per_second_gbit` | yes | lengthen the test |
| 9 | **TCP metrics flush:** `ip tcp_metrics show` empty afterwards, on both VMs | run record: `tcp_metrics` (both VMs, after the flush) | yes | fix |
| 10 | **Sysctls:** set, guarded and reset per visit; after a forced exception both VMs are back at Debian's values (unit test and one live check) | run record: `sysctl` (both VMs, before the clock); the unit test | the guard sees the default before every default run | fix |
| 11 | **The tuned anchor** (revised), scale 5: its visit sets the tuned sysctls at r = 0, and the guard sees them before every run there | run records at `2:0:tuned`: `sysctl`, `condition` | yes | fix |
| 12 | **TLS:** stunnel's log shows TLS 1.3 and its suite; `strace -e setsockopt` shows `TCP_NODELAY` on both of each stunnel's sockets; refusals with no client certificate and with a wrong CA; a `v1_tls` output byte-identical to v1's selection; the stunnel link test within 10% of the direct one at 60 ms default, and at least 0.9 of the link at 120 ms tuned; publication to the receiver's exit about one r in both arms, at 120 ms tuned; the receiver's prefetched bytes recorded in both arms (revised). At scale 70, 120 ms tuned: `v1_tls` − v1 at most 2 round trips + 5% | stunnel's logs; the `strace` output in `logs/`; run records: `verification`, `tool_reported.receiver.publish_s`, `phases` (the receiver's exit), `tool_reported.prefetch.bytes` (revised after the check); trial record: `first_visit.stunnel_link_test` | all | fix the proxy's configuration before the sweep |
| 13 | **pgdelta's switches and timers:** `--window` 1, 4 and 16 and `--high-churn-node-fraction 0.5` reach the statistics, with identical output and page bytes across windows; every timer present; `accept_wait_s` about 1 r in clear and 3 r through the proxy; high-churn files at least 1 in uniform; each wait's busy and idle parts add up to it within 2 ms (revised) | run records: `tool_reported` (pgdelta's statistics: `window`, `high_churn_node_fraction`, `high_churn_files`, `pages`, the timers and their `_busy_s` and `_idle_s`) | all | fix pgdelta |
| 14 | **The `ss` sampler** starts before the clock, stops after it, and yields `minrtt`, `busy` and `rwnd_limited` for every arm's sending sockets (`full`'s walsender, pgdelta's or stunnel's, the `parcopy` arms' 8 or 16, `pg_probackup`'s sshd), and `notsent` and `unacked` for stunnel's WAN socket (revised after the check); the `tc` sampler yields netem's backlog on both VMs | run records: `ss`, `qdisc.samples` | yes | fix |
| 15 | **Changed files and leaf requests** per workload at scale 70 (E9's inputs) | run records: `tool_reported.files.changed`, `tool_reported.leaf_requests` | recorded | – |
| 16 | **`probackup8` at 60 ms,** scale 70: its SSH connections (`ps`), the ticker on the source, the time | run record: `processes`, `phases`, `totals.total_s` | under 120 s | it runs once, in repeat 1, recorded |
| 17 | **The SSH probe** at 60 ms on both host settings | trial record: `first_visit.ssh_probe` | runs; rate recorded | fix the harness |
| 18 | **Durability:** (a) and (b) for `full` at the anchor and at 120 ms default; the idle noise over 20 windows of 5 s, 6 of 30 s and 3 of 120 s, which becomes the run lines' `NOISE` table, one value per window length (revised after the check); `full_nosync` at the anchor leaves most of its copy dirty | run records: `durability` (with each run's limit and window length); `run.json`: `dirty_idle_noise` (every window's increase, by length) | as EXP-11 | as EXP-11; a noise above 14 MiB at a length that covers a hot v1 run is recorded with what check (a) can then no longer catch |
| 19 | **The syscall audit** for v1 and `v1_tls` at scale 5 | `run.json`: `syscall_audit` (the audit's own counts) | 0 uncovered files and directory changes | as EXP-11's smoke item 3 |
| 20 | **Reference digests:** the analysis reads the trial records and reports differences by file class (user tables, catalogs, other) | trial records: `reference_digests` | the report runs; any user-table difference is listed | fix the analysis |
| 21 | **The time projection:** one scale-70 pass over uniform 1%'s nine conditions with every arm once (`parcopy16` at 120 ms default included, with its `verify_direct` and `fsync` count), and the scale-5 overheads | run records: `started_utc`, `finished_utc`, `verification`, `durability` | – | apply the cuts below |
| 22 | **`analysis/exp13.py`** end to end on the smoke CSV, writing to a scratch directory | its output directory | runs | fix |
| 23 | **Clean-up:** `wan.sh off` restores Debian's qdisc (`mq` with `fq_codel`, as recorded) on both VMs | `run.json`: `teardown` (`wan.sh show --json` after `off`) | `tc` shows it | fix |

## Run plan and budget

**Per-run times, bottom-up.** From EXP-11's records, the untimed overhead per run is about 10.8 s for v1 (B0 to B, the stop and restart, SHA-256 of both sides, the start), 7.5 s for a `parcopy` arm, 5 s for a full copy (verification and the start), and 36 s for `probackup8` (the start and the logical digest). Add about 0.5 s per run for the guard, the metrics flush and the samplers. Each visit costs about 12 s (the tree, its checks, a 6 s link test); a trial's first visit to a condition about 25 s more, and 8 s more for the 16-stream test at 120 ms default. A trial's build, B0, workload and end take about 4 minutes (6 for uniform 10%, whose workload is ten times larger), and its reference digests and EXP-11's probes about 1.5 more (revised after the check). The timed seconds come from E4–E12; the model behind them, with these overheads, gives the figures below.

| Step | Runs | Pair wall time |
|---|---:|---:|
| Create both VMs in `us-central1-a`; provision (`startup.sh` with sshd also on port 2222, stunnel4 and strace; `install_incumbents.sh` on both); deploy; SSH set-up; the session CA | – | 20 min |
| Smoke run (above), the 30 s and 120 s idle-noise windows included | about 80 | 49 min (39–59) |
| Invocation 1, uniform 1%: 9 conditions | 112 | 68 min (63–77) |
| Invocation 2, hot: 7 conditions | 84 | 43 min (40–49) |
| Invocation 3, uniform 10%: 6 conditions | 58 | 38 min (35–43) |
| Fetch, then `wan.sh off` and `infra/gcp/teardown.sh` | – | 10 min |
| **Total** | **254 in the sweep, about 335 with the smoke run** | **about 3.8 h (3.5–4.3), about 3.8 USD** |

That is over the ledger's 2 VM hours. Of the sweep's 149 minutes, about 55 are timed (one window-bound `full` copy alone takes 41–102 s at 60–120 ms on default hosts), about 42 are the runs' untimed checks, about 32 are the 110 visits' checks and link tests, and about 20 are the trials' builds, reference digests and probes.

**If the smoke run projects more than 3.5 h,** cut in this order until it doesn't (revised after the check):
1. Hot at 20 ms (−9 runs, about 4 min). The 20 ms verdict is then Not judged, since hot decides it with uniform 1%.
2. Uniform 10% at 3 repeats, its baselines in repeat 2 only (−25 runs, about 12 min).
3. `probackup8` (−3 runs, about 4 min). B_rel is then one stream at every r.
4. `full` at 120 ms default in hot (−2 runs, about 3 min). `full` there rests on uniform 1%'s 2 runs, flagged.

If it is still over after all four, the session runs anyway and the overrun is recorded. At the projection's middle, about 3.8 h, cuts 1 to 3 apply; at its low end, none. A line of about 3.8 h would run the whole design: that is the lead's budget decision, asked for separately. **Never cut:** the anchor; the tuned anchor; every 2 Gbit/s condition of uniform 1% with `parcopy8` and `full`; `parcopy16` at 120 ms default; the 1 Gbit/s conditions; the window and fallback arms; `v1_tls` at the anchor and at 120 ms tuned, since cutting it favours v1 (revised after the check); the negative control; the durability checks. Every cut is recorded under rule 8.

**Run lines** (the shape):

```sh
NOISE=5:4,30:4,120:4   # smoke item 18: dest's idle Dirty + Writeback noise, MiB, per window length in s; a run's check (a) limit is max(4 MiB, the noise of the shortest length that covers its clock)
AT2=2:0:default,2:20:default,2:60:default,2:120:default,2:60:tuned,2:120:tuned
TA=2:0:tuned      # the tuned anchor: v1 and full only
SIDE=2:120:tuned
COMMON="--manifest-checksums CRC32C --dirty-noise-table $NOISE --repeats 5 --control-port 2222 --baseline-methods full,parcopy8,parcopy16 --baseline-repeats 2,4 --link-test-seconds 6 --link-test-warmup 3 --tuned-tcp-max-bytes 134217728 --negative-control full_nosync@2:0:default"
python3 experiments/network/remote.py start exp13-uniform1 -- --patterns uniform --fractions 0.01 $COMMON \
    --conditions $AT2,$TA,1:60:default,1:120:default --order-seed 131 \
    --methods full,parcopy8,parcopy16,probackup8,pgdelta_direct_inplace,pgdelta_direct_inplace_w1,pgdelta_direct_inplace_w16,pgdelta_direct_inplace_hc,pgdelta_direct_inplace_tls \
    --method-conditions full=$AT2,$TA \
    --method-conditions parcopy8=$AT2,1:60:default,1:120:default \
    --method-conditions parcopy16=2:120:default \
    --method-conditions pgdelta_direct_inplace_w1+pgdelta_direct_inplace_w16+pgdelta_direct_inplace_hc=$SIDE \
    --method-conditions pgdelta_direct_inplace_tls=2:0:default,$SIDE \
    --method-conditions probackup8=2:60:default --method-repeats probackup8=1,3,5 \
    --baseline-every-repeat 1:60:default,1:120:default --zero-delay-link-rates 1 \
    --first-visit-link-streams 16@2:120:default \
    --ssh-probe-port 22 --stunnel-link-test 2:0:default,2:60:default,$SIDE
python3 experiments/network/remote.py start exp13-hot -- --patterns hot --fractions 0.01 $COMMON \
    --conditions $AT2,$TA --order-seed 132 \
    --methods full,parcopy8,parcopy16,pgdelta_direct_inplace,pgdelta_direct_inplace_w1,pgdelta_direct_inplace_w16,pgdelta_direct_inplace_tls \
    --method-conditions parcopy8=$AT2 --method-conditions parcopy16=2:120:default \
    --method-conditions pgdelta_direct_inplace_w1+pgdelta_direct_inplace_w16=$SIDE \
    --method-conditions pgdelta_direct_inplace_tls=2:0:default,$SIDE \
    --first-visit-link-streams 16@2:120:default
python3 experiments/network/remote.py start exp13-uniform10 -- --patterns uniform --fractions 0.1 $COMMON \
    --conditions 2:0:default,$TA,2:20:default,2:60:default,2:60:tuned,$SIDE --order-seed 133 \
    --methods full,parcopy8,pgdelta_direct_inplace,pgdelta_direct_inplace_hc \
    --method-conditions parcopy8=2:0:default,2:20:default,2:60:default,2:60:tuned,$SIDE \
    --method-conditions pgdelta_direct_inplace_hc=$SIDE
```

A condition is written `RATE:RTT_MS:HOST`. An arm not named in `--method-conditions` runs at every condition of its invocation. `NOISE` carries check (a)'s idle noise into all three sweeps, as EXP-11's run lines did, now one value per window length; until smoke item 18 sets it, it is 4 MiB at every length (revised after the check). Every invocation selects one of EXP-11's arms (`parcopy8`), so the harness takes the reference digests and the probes in every trial without an option (revised after the check). If A16 says `full` must run at 1 Gbit/s, the first invocation's `full` line gains `1:60:default,1:120:default`, recorded with the projection.

## What it needs built in the harness

About 4.3 days, in this order of priority. The registered prediction and rule, and the verdict, need the first three blocks and v1, `full`, `parcopy8` and `parcopy16`. If the TLS block slips, the run goes ahead without `v1_tls`: A6 is "not judged", and the change is recorded. If `probackup8` slips, B_rel is one stream at every r.

| Block | File | What |
|---|---|---|
| WAN emulation (about 1.1 days) | `infra/gcp/wan.sh` (new; `throttle.sh` unchanged) | `wan.sh RATE DELAY_ONE_WAY_MS PEER_IP CONTROL_PORT`, `off`, `show --json`: the `prio` root, the `u32` filter (to the peer and not port 2222 → the delayed band), netem with `limit 1000000`, its tbf child (`throttle.sh`'s burst, a queue of max(50 ms, r)); a dry-run mode that prints the `tc` commands, for tests |
| | `infra/gcp/startup.sh`, `experiments/network/remote.py`, `run_exp04.py` | sshd also on port 2222; the harness's multiplexed connection on it; stunnel4 and strace in the packages and the preflight |
| | `experiments/network/agent.py` | `wan` (apply, show, off); `ping-check PEER --count 20`; `tcp-metrics-flush`; `ss-sampler start/stop`, and a `tc -s -j` sampler beside it for netem's backlog (revised); `ssh-probe --port 22 --seconds 6`; `snapshot` gains the qdisc counters of both bands and `nstat`; the qdisc parser reports netem's own drops (netem's less its child tbf's, from one snapshot) (revised); `blast-recv` records per-second byte counts for one stream and for several (revised); `blast-send` records each stream's TCP connect time, `connect_s`; the `ss` parser keeps `notsent` and `unacked` (revised after the check); `cmd_env` gains `tcp_rmem`, `tcp_wmem`, `tcp_adv_win_scale`, `tcp_slow_start_after_idle`, `default_qdisc`, and the OpenSSH, OpenSSL and stunnel versions |
| Orchestrator (about 0.95 day) | `experiments/network/run_exp04.py` | `set_condition()` beside `set_speed()`: the tree and the sysctls on both VMs, the visit's checks and A11 (i)'s retry, the per-visit and first-visit probes; the guard and the metrics flush before every run; resetting to Debian's sysctls and no tree in `finally`; conditions per repeat in `run_trial()`, with the tree off outside visits; `--conditions` (the tuned anchor `2:0:tuned` included), `--method-conditions`, `--method-repeats`, `--baseline-methods`, `--baseline-repeats`, `--baseline-every-repeat`, `--negative-control`, `--zero-delay-link-rates`, `--ssh-probe-port`, `--stunnel-link-test`, `--control-port`, `--tuned-tcp-max-bytes`; **revised:** `--link-test-warmup` passed to `blast-recv` as `--warmup`, and the steady rate as the median of the last 3 one-second rates; `--first-visit-link-streams N@CONDITION`; **revised after the check:** `--idle-noise-plan 5:20,30:6,120:3` (window length and count) in place of EXP-11's one length, and `--dirty-noise-table SECONDS:MIB,...` in place of `--dirty-noise-mib`, each run's check (a) limit chosen by its clock and recorded in its `durability`; the trial record's `visits` (tree, pings, control no-ops, link tests with `steady_gbit`, `per_second_gbit` and `connect_s`) and `first_visit` (link tests with their `qdisc` snapshot, `ssh_probe`, `stunnel_link_test`); `run.json`'s `control` (port, multiplexing) and `teardown` (`wan.sh show --json` on both VMs after `off`); the run record's `tcp_metrics` (`ip tcp_metrics show` on both VMs after the flush) and, for `probackup8`, `processes` (a `ps` of both VMs mid-run); `tool_reported()` carrying pgdelta's `window`, `high_churn_node_fraction`, `high_churn_files`, `leaf_requests` and the sender's timers with their `_busy_s` and `_idle_s`, and the receiver's `hello_wait_s`, `recv_wait_s` and `publish_s` under `tool_reported.receiver` (the prefetch stays at `tool_reported.prefetch`); the busy threshold (the visit's one-stream steady rate times its ping minimum) passed to each pgdelta sender; `method_flags()` recording each arm's full command line, the stunnel configs and the `tc` commands in `run.json` |
| | `experiments/network/measure.py` | condition parsing; the new methods in `PGDELTA_ENGINES` (`window`, `high_churn`, `transport: 'tls'`), `parcopy16` beside `parcopy8` (16 streams), `TIERS`, `PROC_SCOPE` (stunnel's processes); CSV fields: `rate_gbit`, `rtt_ms`, `host_tcp`, `ping_min_ms`, `visit_one_stream_gbit`, `zero_delay_gbit`, the netem and tbf counters, `netem_own_drops`, `netem_backlog_max`, retransmissions, the `ss` readings, the protocol timers with their `_busy_s` and `_idle_s`, `prefetch_bytes`, `prefetch_s`, `tls_version`, `tls_suite`, `excluded_reason`, `inferred`, `window_limited_bound`; **revised after the check:** `durability_check` takes each run's own limit; CSV fields `noise_window_s`, `dirty_limit_bytes`, `connect_s`, `notsent`, `unacked` |
| pgdelta (about 0.75 day) | `pgdelta/cli.py`, `pgdelta/sender.py` | `--window N` on `send` (default 4, today's value), an experiment switch like `--high-churn-node-fraction`; the sender's `connect_s` (revised after the check), `accept_wait_s`, `basis_wait_s`, `leaf_wait_s` (and waits over 1 ms), `page_send_s`, `complete_wait_s` and leaf requests in its statistics; **revised:** each wait split into `_busy_s` and `_idle_s` by reading the socket's `SIOCOUTQNSD` and `SIOCOUTQ` every 1 ms while it waits, against `--busy-inflight-bytes` (an experiment switch; without it, only unsent bytes count as busy) |
| | `pgdelta/receiver.py` | `hello_wait_s`, `recv_wait_s` and `publish_s` in the receiver's statistics |
| TLS stand-in (about 0.25 day) | `agent.py`, `run_exp04.py` | `tls-material`: a session CA and two certificates with `openssl`, copied to the source over the control path; `stunnel start/stop` with the generated configs, stunnel's default `TIMEOUTclose` (revised); the log parsed for protocol and suite; `run_pgdelta_direct` through the pair for `v1_tls` (receiver on localhost, sender to the local stunnel) |
| `pg_probackup` at latency (about 0.25 day) | `run_exp04.py` | the WAL ticker on the source over its Unix socket, launched over the control path; A15's void rule (user-table files only), with catalog differences reported by file class |
| Analysis (about 1 day) | `analysis/exp13.py` (new), `analysis/exp13_model.py` (new, revised), `experiments/network/report.py` | pooled baselines and per-trial readings; Δ from each host setting's anchor and effective round trips; B_fast, B_rel and F0 per condition; `R_fast`, `R_rel`, `R_F0`, `R_adj`, `R_max`, `R_trial`, `R_trig`, `R_tls`; L1, L2 and R1, with R1 first; the verdict's rows, flags and qualifier; A2–A17, A6's two readings among them; crossovers (`analysis/exp04.crossover` with a baseline, x from the zero-delay tests); A10's drift table against `exp11-cells.csv`; the network table (link tests against the model, `ss`, netem and tbf counters, exclusions); figures `exp13-rtt.svg` (time against r, per workload and host setting) and `exp13-ratios.svg`; `exp13-runs.csv` and `exp13-cells.csv`. `exp13_model.py`: the derivation of E3–E14 as code, whose only inputs are W and the one-stream steady rates. **Revised after the check:** A3's four parts with the residual; `R_tls` and the TLS-sensitive flag on A6's reading; A9's flag against the zero-delay rate at each tbf rate; the "v1 not durable" row and row 4 over the settings measured; a check that every Record path in the smoke table exists in the smoke run's records. Dry-run on the smoke data before the sweep |
| Tests | `tests/test_exp13_core.py`, `tests/test_wan_tree.py` | `wan.sh`'s `tc` lines and the filter, and the parser on real `tc -s -j` output in which tbf's drops show in netem's counter (captured on a Debian 12, kernel 6.1 host before the session) (revised); condition parsing and the run lines exactly as written here; the visit's checks and A11's exclusions; the sysctl guard and its reset after an exception; the metrics flush before every run; stunnel's config contents; `method_flags` carrying each new arm's command line; `--window` reaching the sender, with identical output for 1, 4 and 16; the timers present and growing on a slowed local channel, and on a slowed local TCP connection a wait whose busy part grows while bytes are unsent, with busy plus idle equal to the wait (Linux; the queue readings stubbed elsewhere) (revised); `blast-recv`'s per-second counts and the median-of-last-3 rate (revised); `exp13_model.py`'s ranges, from the inputs written here, falling inside the ranges written here (revised); the pooled baseline choice; A16's inferred cells; the void rule; the verdict's rows on synthetic medians, every row reachable, R1's precedence and A2's mixes included (revised); **revised after the check:** each run's check (a) limit chosen from the noise table by its clock; `tool_reported()` carrying every field the smoke table names, on a synthetic statistics file; A3's parts adding up to Δ_v1 on synthetic timers; a lost tuned cell reaching row 4 and a v1 lost to A13 reading "v1 not durable" |

Docs: a "Methods for T12" section in `experiments/network/README.md`, this write-up as `docs/experiments/EXP-13-latency-transport.md`, and a row in the experiments index.
