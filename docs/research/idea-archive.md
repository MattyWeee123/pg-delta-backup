# The idea archive (rule 14)

Every idea tried or proposed, with where it won, where it lost, and what should make us look at it again. Each research-loop pass reads this first and proposes at least one structurally different idea beside any tuning. DGM's ablation found an archive beats always building on the latest version, and single-path search stays anchored near its start (the drift caught on October 27).

**Status:**
- **default:** on by default (with `--fast-path` where noted);
- **opt-in:** built, off by default;
- **in test:** being decided by an experiment;
- **unbuilt:** proposed, not built;
- **open:** a measurement owed;
- **rejected:** with the evidence.

**Family:**
- **algorithm:** what is read or sent;
- **systems:** how it is done;
- **measure:** a comparison or model.

## The fast path (labels from WAL summaries)

| Idea | Family | Status | Evidence | Won where / lost where | Revisit when |
|---|---|---|---|---|---|
| Labels from WAL summaries instead of hashing B | algorithm | opt-in (`--fast-path`) | EXP-61–64, -79b | Beyond memory: 0.022 of a full copy for local changes, wins up to about 91% of pages / past 91% `pg_basebackup` wins (same bytes) | `summarize_wal` becomes common enough to make it the default |
| Guard v2: B's end proven by how it stopped | algorithm | default | EXP-62 | Closed S2d's wrong pages / crashed copies hashed | Rows "crashed standby" and "crash-recovered copy" below |
| Reader threads (`--read-ahead 8`) | systems | default (fast path) | EXP-64b | 164.8 → 102.8 s at 28% of pages | — |
| fsync on a worker thread | systems | default (`auto`, fast path) | EXP-68 | 102.8 → 76.5 s | — |
| Fill clean gaps of up to 2 pages | systems | default (fast path) | EXP-70, -74 | 76.6 → 46.6 s where requests bind / no gain, more bytes where bytes bind (fill 0 fastest at 50%+ of pages) | The selector chooses it per plan once it is on by default |
| Old primary after a failover from its own WAL (idea 1) | algorithm | default (fast path) | EXP-69, -69b, -71 | 0.20 of `pg_rewind` (hot), 0.11 (scattered) / needs checksums or `wal_log_hints`, and B's WAL since the fork | — |
| Selector: a time model per plan picks the fill | measure | opt-in (`--selector`) | EXP-74, -75; held out: EXP-84 | Within 2.6% out of sample; took the fastest fill; held out, `pg_basebackup` within 1% on pd-balanced and the full-copy call right in 10 of 10 / held out, every refresh of 1.5–2.5 s under-predicted by 18–33% (its fixed cost was fit only on long refreshes), and pd-ssd over-predicted 1.38–1.52x | Met (EXP-84's H3): measure the disks' limits at the session's start, refit the fixed cost on short refreshes, then new held-out cells; then default |
| A full copy as the selector's option | algorithm | rejected | EXP-75's registration | — / pgdelta's whole-file path reads every file twice, and can't beat fill 0 by the model | A whole-file streaming path that pays no per-page cost |
| Guard: one grouped query (B) | systems | default | EXP-73, -73b | 0.41–0.47 of per-summary reading | — |
| Guard: blocks unsorted with duplicates (D) | systems | default | EXP-78, -79, -79b | 0.46 locally, 0.55–0.57 on the VMs | — |
| Guard: the query split over 4 connections (D4) | systems | unbuilt | EXP-78 (reported) | 0.70 of D's time locally / 3 more connections to the source in backup mode | The guard is again the largest fixed cost after the selector's refit (now 2.9 s of 5.1 s at 96% of pages) |
| Written pages re-read a run at a time, on the verifier | systems | default | EXP-76, -77 | The stall at every 1 GiB file gone; 113.2 → 108.5 s | — |
| Early write-back on the sync worker (`--fsync worker-writeback`) | systems | opt-in | EXP-76 | 1.81 s faster where requests bind / 3.75 s slower where bytes bind | As a selector option for request-bound plans; re-time since EXP-77 removed the stall it interacted with |
| A crashed standby proven by its minimum recovery point (`--crashed-standby`) | algorithm | opt-in | EXP-80 (local), EXP-80b (VMs) | 100 of 100 randomized crashed standbys correct; beyond memory 0.023–0.233 of hashing's time, 0.02–0.46 of a re-clone / crashed primaries still refused | Held out (EXP-84): 10 of 10 cells proven, correct, within +0.36 s or 0.9% of a clean standby. Eligible for a default with the fast path: the owner's decision |
| Crash recovery first (`pg_rewind`'s step) | algorithm | rejected | EXP-80, S2e | — / reopens EXP-62's blind spot: S2e accepted and wrong in 3 of 3 builds | — |
| Detecting a crash-recovered old primary (its end-of-recovery checkpoint in its own WAL) | algorithm | unbuilt | EXP-80's registration (residual risk) | Would close a blind spot guard v2 and `pg_rewind` share | A cheap, reliable WAL signature is found |
| Labels from a scan of S's WAL by pgdelta (summaries off, or PostgreSQL before 17) | algorithm | superseded on 17+ | Research loop 2, 3 | Summaries on demand do it with PostgreSQL's own summarizer / still the route before 17 (pgdelta already parses `pg_waldump` for `--diverged-basis`) | A PostgreSQL 16 user |
| Page-LSN selection on the source (`--fast-path page-lsn`; summaries off; WAL-G's and `pg_probackup` DELTA's way) | algorithm | opt-in | Research loop 3; EXP-82: 120 of 120 correct; EXP-82b beyond memory: 0.470 (uniform) and 0.999 (hot) of hashing, 0.81 and 0.998 of WAL-G, 0.97–0.98 of a re-clone | Beats hashing for scattered changes and WAL-G's catch-up, ties both for local ones / ties a re-clone (reads all of S); needs checksums or `wal_log_hints` | Held out (EXP-84): 0.86–0.98 of a re-clone in 9 of 10 cells; at scale 1,200 (4.5x memory) with 28% of pages changed, 1.12x: it read 1.108x S, re-reading chosen pages that had left the cache (post-hoc). See single-read page-lsn |
| Single-read page-lsn: send each chosen page from the scanner's buffer, never re-reading it | systems | unbuilt | EXP-84 (post-hoc) | page-lsn would read each page of S exactly once, so never more than `pg_basebackup` / memory for the chosen pages of the lookahead (up to 2 files' worth) | Built and timed on H2's spent cell (scale 1,200, uniform), then new held-out cells |
| Summaries on demand (`summarize_wal` turned on at refresh time; PostgreSQL 17 summarizes from the oldest WAL on disk) | algorithm | measured (a setting, no code) | Research loop 3; EXP-82: 20 of 20 correct; EXP-82c beyond memory: 0.045 (hot) and 0.544 (uniform) of hashing's time; the summarizer reads at the disk's rate (203 MB/s) | 3.95 s for local changes / slower than page-lsn where the WAL since B nears \|S\| (1.14 in uniform); needs that WAL kept; a server setting the operator changes | A selector between it and page-lsn by W against \|S\| |
| B's own WAL for the part of its redo-to-end span the server no longer holds (with summaries on demand: a slot keeps WAL only from where the standby stopped, past its redo point) | algorithm | unbuilt | EXP-82b's design | Would let summaries on demand cover a standby held by a slot, as `--diverged-basis` already reads B's WAL / more of B's WAL read on the receiver | Summaries on demand win on the VMs |
| Advise `summarize_wal = on`, so every later copy takes the summaries path | measure | firm in memory; a stated price beyond it | EXP-83 to -83d | No measurable cost where the database fits in memory; the summarizer 0.8–1.0 ms of CPU per MB of WAL, its WAL read from the page cache / beyond memory, disk-bound: p99 up about 20% in steady state, the data disk writing 24% more (cache displacement, post-hoc) | A CPU-bound source (synchronous_commit off or a fast WAL disk); the summarizer's memory; a plain WAL reader with summaries off |
| Skipping S's files by modification time (pgBackRest's and rsync's shortcut) | algorithm | rejected for now | Research loop 3 | Would read only files written since B's checkpoint / sound only if S's clock never steps back and no tool restores old mtimes | A sound source of file change times |
| An offline evolutionary search over saved plans (OpenEvolve-style) | measure | unbuilt | RSI review | Cheap evaluator (the time model over saved plans) / the model only ranks; VMs decide | A small policy with a numerical objective is the bottleneck |
| A CI test that enforces the workflow's checkable parts (registration before data, integrity records, the noise floor current, guarded defaults, the workflow check present) | measure | deferred (the owner, October 29: instructions are enough for now) | RSI review (evaluators outside the model) | Would catch an instruction-only rule that slips / checks form, not honesty; editable by the agent unless review is required | A rule in [WORKFLOW.md](../WORKFLOW.md) is found broken in a write-up |

## Hashing, transport and verification

| Idea | Family | Status | Evidence | Won where / lost where | Revisit when |
|---|---|---|---|---|---|
| Keyed hash tree of 8 KiB pages, in place, verified | algorithm | default (without the fast path) | EXP-03, -09, -17, -25 | DR links: 0.33–0.75 of a full copy / beyond memory at best a tie (it reads all of B) | — |
| BLAKE3 | systems | default where installed | EXP-60 | 0.87 of BLAKE2b at 5 Gbit/s | — |
| BBR on pgdelta's own socket | systems | opt-in | EXP-24, -26 | Holds at DR round trips / took 81–96% of a shared link from cubic flows | — |
| zstd on a pool | systems | opt-in | EXP-45–53 | 0.52 of v1 at 0.5 Gbit/s / break-even about 4 Gbit/s | — |
| Sub-page deltas (512-byte blocks) | algorithm | opt-in (off) | EXP-54–56 | 3.5x fewer bytes / slower as built (block hashing cost) | Native node-first hashing is built |
| Native node-first hashing with BLAKE3 chaining values (idea 3) | systems | unbuilt | Research loop 1 | — / the hash path already runs at 85% of its ceiling | A VM split shows per-page work, not reading, dominates |
| One-pass verification (`--verify-scope written`) | systems | default (in place) | EXP-32, -33, -35 | No wrong copy in 3,000 trials | — |
| pgdelta's own mutual TLS 1.3 | systems | opt-in (`--tls-*`) | EXP-58, -72 | About 2% at 2 and 5 Gbit/s after the transport fix | — |
| IBLT set reconciliation | algorithm | rejected | Research loop 1 | — / still needs every page read | — |
| Content-defined chunking, cross-file matching | algorithm | rejected | EXP-37 | — / no reuse after `VACUUM FULL` by any strategy | — |

## Measurements owed (from research loop 2's scorecard)

| Comparison | Status | Prediction (inferred) | Plan |
|---|---|---|---|
| The fast path against native incremental + `pg_combinebackup` (backup chains) | **done (EXP-81)** | Held: 0.29 (hot), 0.47 (uniform) against `--copy-file-range` on XFS, its best mode (a synced capture; `--clone` 4x slower in a pilot) | — |
| The fast path against WAL-G catch-up | **done (EXP-81)** | Held: 0.019 (hot), 0.385 (uniform); WAL-G reads every file in full on the source | — |
| The fast path at DR round trips | open | Wider wins than hashing's | With the next DR session |
| A cloud disk clone plus recovery, same zone | open | The clone wins for large changes, the fast path for small lag | When the cloud HA story matters to the sponsor |
