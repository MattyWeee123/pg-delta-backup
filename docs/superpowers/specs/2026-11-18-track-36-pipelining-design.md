# Track #36 — pipelining: design

Status: **approved** by the owner, November 18, 2026. Scoped with step 3 (#19) gated on a funded
shaped-latency session; steps 1 and 2 proceed now.

Owner: SamEthanMathew. Issue: [#36](https://github.com/MattyWeee123/pg-delta-backup/issues/36).
Problem analysis and evidence: the measured bottleneck is set out in
[#36's first comment](https://github.com/MattyWeee123/pg-delta-backup/issues/36#issuecomment-6989582048);
phase-1 evidence is in `docs/PAST-RESULTS.md` and, in the archive,
`docs/experiments/EXP-{21,23,90,91,92}.md` plus EXP-94 on branch `exp88-slowdown-by-load`.

---

## 1. Intent and why

The engine on `main` is a stop-and-wait protocol: one `CHUNK_HASH` → `VERDICT` round trip per 8 KiB
chunk, on one thread, with the receiver reading B's chunk only when asked. Its throughput ceiling is
`chunk_size / RTT` regardless of link bandwidth. On the team's own cloud run (PR #31) that is 86% of
the wall clock — 37.20 s of 43.41 s — and the receiver's entire contribution to all 41,174 questions
is 1.5 s. The time is latency, not work.

This track removes the waits, one at a time, each with a benchmark showing what it bought. That
sequencing is the reason `main` was restarted from a naive version on November 13, and it maps to the
mentor's October 30 ask: batch the hashes, have both sides hash at once, keep the pipe busy.

## 2. Goals

- **G1.** No per-chunk round trip remains. Round trips become O(files), then O(1) per session.
- **G2.** Each step lands as its own pull request with a published benchmark on the same setup as the
  run it improves on, and a stated figure for what it bought.
- **G3.** Every refreshed copy still equals S byte for byte, and every existing correctness property
  survives unchanged.
- **G4.** The naive behaviour stays reproducible from the same binary, so "naive vs pipelined" is one
  flag and not two checkouts.

## 3. Non-goals

- **Coarse-then-fine digests / hash tree.** That is issue #24, parked with label `later`. A flat leaf
  exchange is enough for this track. Do not import v1's `BASIS_NODES` / `LEAF_REQUEST` tree.
- **Parallel TCP connections or worker processes.** Issue #37, another owner's track.
- **A faster hash function.** Issue #38.
- **Verify-as-written.** EXP-91 bundled this with hash-ahead, but the restart split it out: it is
  issue #25, folded into track **#40**. This track moves the receiver's *fsyncs* off the main loop
  (which #19 asks for) and leaves the read-back verification alone.
- **The 1,000-trial randomized correctness campaign.** Track #40 owns it. This track keeps the
  existing tests green and supplies per-PR evidence.
- **Porting v1's engine.** Its *design* and its `waits.py` instrumentation are reference material;
  its code is not imported.

## 4. Anti-goals — what would make this a failure even if it works

- **A faster engine with no per-step attribution.** If the three steps land as one lump, the track has
  produced a speedup and destroyed the reason the repo was restarted. Each step's number is the
  deliverable.
- **A benchmark that cannot see the thing it scores.** Step #19's gain is zero at zero RTT (EXP-94
  measured 0.990 and 1.006). Scoring it on the laptop or on a same-region 1 ms fixture would
  "disprove" a real effect. See §9.
- **Silent correctness drift.** Any step that makes the negative-control test (`os.pwrite` corruption)
  pass is a failure regardless of its timing.
- **Unbounded memory.** A batch or a digest window that scales with database size rather than with a
  configured bound.

## 5. Constraints (from the code as it stands, commit 78c8c5e)

- **Files arrive in order.** `receiver.py`'s `open_file` closes the previous file when a new index
  appears — "files arrive in order: the previous one is finished". **No step may reorder files on the
  wire.** Within-file reordering is also ruled out for step 1 and 2 by the same handle logic.
- **Only `chunks_same + chunks_changed` cost round trips.** Chunks B lacks (`chunks_new`: a new file,
  or past B's end) are already sent without asking. The batching boundary has to respect that split —
  this is the "batches that straddle file ends" case #17 names.
- **`--chunk-size` is a live option** (512 B to 16 MiB, default 8192) and `test_larger_chunks` runs at
  32768. Batch size and chunk size must stay independent, and the batch must be bounded in **bytes**
  as well as in count.
- **The whole-file digest is computed streaming, in order,** as S is read. Batching must not disturb
  it.
- **`protocol.VERSION` is checked on HELLO** and the receiver refuses a mismatch. New message types
  mean sender and receiver must be deployed as a pair.
- **The benchmark harness reads `bytes_source_to_dest` / `bytes_dest_to_source`** (`stats.py`, noted in
  the code) and `viz` reads the event stream. Both must keep working.
- **`local` runs both ends over a `socketpair` in one process** with the receiver on a thread. Any new
  thread must not deadlock that arrangement, and `waits.py`-style send-queue readings are unavailable
  there (no TCP socket).

## 6. Approaches considered

| Approach | What it is | Trade-off | Verdict |
|---|---|---|---|
| **A. The roadmap, three measured steps** | #17 batching, then #18 streaming, then #19 overlap, each its own PR with its own benchmark | Slowest to the endpoint, and #17's wire format is superseded by #18's | **Chosen.** It is the only one that produces the per-step evidence the track is defined by, and steps 1–2 are measurable on the laptop demo at no cloud cost |
| B. Jump straight to streaming (#18) | One protocol change; batching kept only as a measurement arm | Faster, less churn, but loses the step-1 datapoint the issue's "done when" asks for | Rejected: answers a question the team did not ask |
| C. Port v1's sender/receiver | 1269-line sender already at 0.706× `pg_basebackup` | Fastest to a working endpoint; drags in #24 and #37, unmeasurable step by step | Rejected: defeats the restart |

Chosen by the owner on November 18.

## 7. Step 1 (#17) — batch the hashes

### Wire format

Two new message types, `protocol.VERSION` → 2:

```
CHUNK_HASHES = 10   sender -> receiver
    head: >IQI  (file index, first chunk index, count)
    body: count * 16-byte digests
    All count chunks are contiguous, in one file, and every one of them lies
    wholly within B's size for that file.

VERDICTS     = 11   receiver -> sender
    head: >I    (count, echoed for validation)
    body: ceil(count/8) bytes, a bitmap, LSB-first within each byte;
          bit i set means chunk (first + i) DIFFERS from B's.
```

`CHUNK_HASH` and `VERDICT` are **retired**, not kept in parallel: two code paths for the same question
is how the engine gets a silent divergence. `--batch 1` reproduces stop-and-wait semantics through the
new messages (one digest, a one-bit bitmap), which is what G4 needs and costs 7 extra bytes per chunk
against today's frames.

The bitmap is why `VERDICT` traffic collapses: 41,174 verdicts cost 247 KB today as 6-byte frames and
5.1 KB as a bitmap. Digest traffic roughly halves too (33 bytes/chunk → 16 bytes/chunk plus one head
per batch), so step 1 is a **bytes** win as well as a time win, and the PR should report both.

### Sender

Replace the per-chunk block with an accumulator over the current file:

1. Read and hash each chunk as today; keep feeding the whole-file digest in order.
2. If the chunk lies wholly within `basis_size`, append `(chunk_index, digest, data)` to the batch.
3. If it does not, **flush the batch first**, then send `CHUNK_DATA` for this chunk (`chunks_new`), so
   wire order within a file stays monotonic in chunk index.
4. Flush when any of: the batch holds `--batch` chunks; the batch holds `--batch-bytes` of chunk data;
   the file ends.
5. Flush = send `CHUNK_HASHES`, wait for `VERDICTS`, then send `CHUNK_DATA` for each set bit, in
   ascending chunk order, from the data held in the batch.

**Memory is bounded by `--batch-bytes`, not by `--batch`.** A batch of 1024 at the default 8 KiB is
8 MB, but at `--chunk-size 16M` it would be 16 GB. Default `--batch-bytes 32M`, and the effective
batch is `min(--batch, --batch-bytes / chunk_size)`. Holding the data avoids re-reading it after the
verdicts arrive — EXP-92 measured double disk reads as a real cost (the source's disk went 88 s →
98–107 s when the planner outran the page cache), so re-reading is the wrong trade even though it
would use less memory.

### Receiver

`CHUNK_HASHES` handler: `open_file(index)`, then for each of `count` chunks compute its span, `pread`
B's range, hash, compare; copy B's bytes into O for the chunks that match (as today) and set the bit
for those that differ. Reply once with `VERDICTS`. Reject, as `ProtocolError`: a count of zero, a
count whose body length disagrees, any chunk past the file's end, any chunk outside B's size, a batch
whose file index is already `done`.

### Counters

`round_trips` changes meaning — it becomes the number of batches. To keep the published runs
comparable, **add** `chunks_asked` (what `round_trips` used to count) and keep `chunks_same`,
`chunks_changed`, `chunks_new` exactly as they are. Add `batches` as an alias-free name and report
`--batch` in the report next to `chunk_size`.

### Option surface

```
--batch N            chunks per question (default 128; 1 reproduces stop-and-wait)
--batch-bytes BYTES  cap on chunk data held per batch (default 32M)
```

Both on `local` and `send`. The receiver needs neither: the batch size is implicit in each message.

## 8. Step 2 (#18) — both sides hash at the same time

This is rsync's generator, and it removes the request/reply dependency rather than dividing it. It is
the step that matters: batching at N = 1024 still cannot fill a 120 ms pipe, which needs 1,831 chunks
in flight (see `notes/track-36-pipelining.md` §4).

### Wire format

```
BASIS_DIGESTS = 12  receiver -> sender
    head: >IQI  (file index, first chunk index, count)
    body: count * 16-byte digests of B's chunks

CHUNK_COPY    = 13  sender -> receiver
    body: >IQI  (file index, first chunk index, count)
    "chunks first..first+count-1 of this file are unchanged: copy them from B into O."
```

`BASIS_DIGESTS` is sent **unasked**, in the receiver's own file order, which is the same order as
HELLO's entry list. `CHUNK_HASHES` and `VERDICTS` are retired in turn. The sender now sends
`CHUNK_COPY`, `CHUNK_DATA` and `FILE_DONE`, and round trips per session fall to the HELLO/ACCEPT pair
plus COMPLETE.

### Why `CHUNK_COPY` exists

Today the *receiver* decides a chunk matches, and copies B's bytes into O as part of answering. Once
the sender does the comparing, the receiver no longer knows which chunks matched, and there are only
three ways to resolve that:

- **The receiver writes every chunk of B into O as it hashes,** and lets `CHUNK_DATA` overwrite the
  differing ones. Correct — `pwrite` is positional and the hashing thread always runs ahead of the
  data — but it writes the whole basis a second time. At EXP-92's 28%-changed workload that is a
  28% write amplification on the destination, and EXP-92 found **the destination's disk is what binds
  once the stalls are gone.** Rejected for that reason.
- **The receiver defers and asks.** Reintroduces a round trip. Defeats the step.
- **The sender streams copy instructions** — `CHUNK_COPY`, one-way, no wait, run-length encoded over
  consecutive unchanged chunks. Write volume on the destination is identical to today's. **Chosen,**
  and it is what rsync does: its sender streams match instructions with offset and length alongside
  the literal data.

A run of unchanged chunks therefore costs one 16-byte frame regardless of its length, which makes the
unchanged-file case nearly free on the wire: an untouched 1 GiB file becomes one `CHUNK_COPY` plus one
`FILE_DONE`.

### Both sides need two threads — this is the deadlock

The naive engine is safe because it is strictly alternating. Streaming in both directions at once is
not, and the failure mode is precise:

- the **receiver** blocks in `send(BASIS_DIGESTS)` because its socket send buffer is full;
- the **sender** blocks in `send(CHUNK_DATA)` because the receiver is not draining its receive buffer;
- neither ever returns.

So: the **sender** gets a reader thread that drains `BASIS_DIGESTS` into a `(file, chunk) → digest`
table while the main thread hashes S and writes `CHUNK_DATA`; the **receiver** gets a hashing/sending
thread for B's digests while its main loop keeps receiving and writing. One writer per direction, and
each side always has a thread in `recv`.

### Flow control — the bound that keeps memory finite

With a reader thread draining eagerly, the digest table grows to the whole of B. Bound it: the
sender's reader thread stops reading once the table holds more than `--digest-window` bytes of
undelivered digests, which fills the socket buffers and blocks the receiver's hashing thread. TCP
provides the backpressure; the window makes it explicit.

Default: `--digest-window 64M` of digests. At 16 bytes per 8 KiB chunk that is 4 million chunks,
32 GiB of basis — ample, and bounded. Note EXP-92's warning that lookahead depth is a genuine
trade-off and not a bigger-is-better knob: its 2 GiB hash-ahead window removed double reads but made
the two disks take turns and ran 1.29× slower than 64 GiB. So `--digest-window` is a measured knob in
step 2's benchmark, not a guess to ship.

### Ordering and the missing-digest case

The sender needs a file's digests before it can decide that file's chunks. It processes files in the
same order as the receiver, so the digests it needs arrive first. Two cases to handle explicitly:

- **B lacks the file entirely** (not in `basis_sizes`): the receiver sends no digests for it; the
  sender sends every chunk as `chunks_new`, as today.
- **B's file is shorter**: the receiver sends digests only for the range it has; chunks past that are
  `chunks_new`.

A digest for a chunk outside the ranges ACCEPT declared is a `ProtocolError`, as is a chunk the sender
reaches with no digest and no reason to treat it as new.

**Invariants the receiver's two threads must hold**, since both now touch O's file descriptors:

- Every write to O is positional (`os.pwrite`), so the two threads never share a file offset.
- `CHUNK_COPY` and `CHUNK_DATA` for a given chunk are mutually exclusive and arrive exactly once, so
  no chunk is written twice and no chunk is left unwritten. `FILE_DONE`'s whole-file digest check is
  what catches a violation, and it already exists.
- The hashing thread may be reading B for file *n+1* while the main loop writes O for file *n*. B is
  opened read-only and separately from O, so this is safe, but `open_file`'s "the previous file is
  finished" assumption now belongs to the **main loop only** — the hashing thread must keep its own
  basis handles and must not touch `files`.
- The fsync worker (step 3) is the third toucher; publication waits on all of it before the rename.

**`local` mode is the harsher deadlock test, not the easier one.** `socketpair` buffers are far
smaller than a tuned TCP socket's (tens of KB against megabytes), so the deadlock in §8 shows up
sooner there and in one process. The deadlock test should run both over `local` and over a real
socket.

**`EventLog` becomes multi-writer.** The sender emits progress from its main loop today; step 2 adds a
reader thread and step 3 a planner thread. Keep emission on one thread (the sender's main loop) or
guard it with a lock — `viz` parses the stream as JSON lines and an interleaved write corrupts it.

## 9. Step 3 (#19) — overlap reading, hashing and sending

Three stalls, which are exactly EXP-90/91/92's three, in the order they were found:

1. **The sender stops hashing while a send blocks.** EXP-90: 93 s of 186 s blocked in `send()`. Fix: a
   planner thread hashes up to `--hash-ahead` bytes of files ahead of the file being sent; the sending
   thread stays the **only** writer on the socket (v1 built it this way round deliberately — only a
   single writer keeps a file's frames in order).
2. **The receiver fsyncs each file in its main loop.** EXP-91: 78.5 s of 151.5 s. Fix: fsyncs go to a
   worker thread; publication still waits for every one of them before the rename.
3. **Pages sent may have left the page cache**, so they are read twice. EXP-92 measured the source's
   disk going 88 s → 98–107 s. Mitigation is the window size, which is why `--hash-ahead` is a
   measured knob and the default is not assumed.

### Instrumentation must land before the scoring

`Stats.timed` cannot see this. Port v1's `waits.py` split — `page_send_s` (time blocked in `send()` on
page data, counted apart from every other frame) and the busy/idle split of each wait, read from the
socket send queue every 1 ms via the Linux ioctls `SIOCOUTQNSD` (written-not-sent) and `SIOCOUTQ`
(written-not-acked). Degrade cleanly: no readings on non-Linux or on `local`'s socketpair, totals kept,
busy/idle null. This is a prerequisite commit in step 3's PR, not a follow-up, because:

**EXP-94's two measurement failures were both instrument failures, and they are cheap to repeat.**

- **79–93% of the time saved lands in `page_send_s`,** against 1–11% across all four protocol waits
  combined. The idle wait is the trigger; the cost is what cubic does afterwards. A wait-timer bar is
  the wrong instrument — EXP-94's prediction 6 set one at 0.25× and "failed" while the arm went 4.8×
  faster.
- **A hot/clustered change is not a valid negative control.** Stalls are per *file*, not per changed
  page — `leaf_requests` was 19 in every cell, hot and uniform alike. EXP-94's prediction 3 assumed
  hot had nothing to gain and failed. **The correct negative control is RTT = 0,** where the measured
  gain is 0.990 and 1.006, i.e. nothing.

### Step 3 needs a latency setup, and that is a decision, not a detail

Neither current fixture can see this step: the laptop has no network and PR #31's GCP pair is
same-region at 1.053 ms. Both are ~0 ms for this purpose. Seeing it needs shaped latency at 20–120 ms
via `infra/gcp`, i.e. a real GCP session (EXP-94's own cost: two `n2-standard-8`, ~105 minutes of
scored runs, ~2 h 05 m of VM time).

**My call, stated for the owner to override:** land #17 and #18 first, both scored on the laptop demo
and one same-region cloud run, since that is where ~37 of the 43 seconds are. Hold #19 until a latency
session is funded, and say so on the issue rather than scoring it on a fixture that cannot show it.
This keeps the track honest and defers the only part that costs money.

## 10. Step 4 (#20) — benchmark and the floor

Per setup, a table of naive (`--batch 1`) vs step 1 vs step 2 vs `pg_basebackup`, from
`python -m bench run --publish` so each lands as its own run PR.

For each run compute the **floor**: the longest time any single resource needed for its own work —
each disk, the link, the CPUs — from the 0.1 s samplers (`experiments/network/util_series.py`). Report
actual against floor and name what set it.

Worked example, PR #31's setup, from its own `stats-pgdelta.json`: sender work 4.25 s (hash 0.63,
read 1.31, send 2.30, scan 0.03); receiver work 7.2 s (fsync 3.25, verify 1.24, write 0.71, read_basis
0.82, hash 0.69, send 0.50); link 293.9 MB at 1 Gbit/s = 2.35 s. **Floor ≈ 7–10 s, set by the
receiver's disk,** against `pg_basebackup`'s 6.78 s and the naive engine's 43.41 s. That is the number
steps 1–2 are chasing, and it predicts near-parity rather than a win — which should be said plainly in
the write-up rather than discovered by a reader.

**Caveat to carry into the report:** PR #31's fixture is suspect and belongs to #39 — it sent 47% of S
because `chunks_new` was 35,236 against `chunks_changed` of 395, so only 395 chunks genuinely differed
and ~288 MB was simply absent from B. Round-trip counts are unaffected (they follow
`chunks_same + chunks_changed`), so the timing conclusion stands, but a corrected fixture will make the
round-trip share **rise**, not fall, because `chunks_new` costs no round trip.

**`--batch 1` reproduces the baseline's accounting, not its byte count.** Per asked chunk the pre-#36
engine sent 33 B (`CHUNK_HASH`) + 6 B (`VERDICT`) = 39 B; `--batch 1` sends 37 B (`CHUNK_HASHES`, 16-byte
head) + 10 B (`VERDICTS`, 4-byte head plus one bitmap byte) = 47 B — about 20% more ask traffic. Counters,
frame sequencing and timer buckets reproduce exactly, so the *accounting* the baseline exists for is
sound; but a "bytes saved against the baseline" figure taken from the `--batch 1` arm reads roughly 20%
flattering on ask traffic. For byte claims, compare against the pre-#36 binary or state the offset.
Raised by the final whole-branch review, 18 November, and recorded here because the sweep that would use
that arm as its baseline is deferred — whoever runs it will not have seen that review.

## 11. Test plan

Existing tests that change **by design**, with what replaces them:

| Test | Today | After step 1 |
|---|---|---|
| `test_identical_copies_send_no_chunk` | `round_trips == 5` | `chunks_asked == 5`, `round_trips == ceil(5/batch)`; still no `CHUNK_DATA` |
| `test_one_changed_page_is_the_only_chunk_sent` | `chunks_changed == 1`, exact `CHUNK_DATA` bytes | unchanged; `CHUNK_DATA` framing is untouched |
| `test_new_files_grown_and_shrunk_files` | `chunks_new == 3 + 2` | unchanged — and this is the straddling case, so add a batch that ends exactly at B's size and one that would have spanned it |
| `test_larger_chunks` | `--chunk-size 32768` | unchanged, plus a case where `--batch-bytes` binds before `--batch` |
| `test_a_wrong_write_is_caught_and_no_output_appears` | negative control | **must stay green at every step, unmodified** |
| `test_send_and_receive_on_localhost` | one real socket | unchanged; step 2 adds the deadlock test beside it |

New tests:

- **Step 1:** batch boundaries — file end, B's size boundary, a short final batch, `--batch 1`
  equivalence (same `chunks_same`/`chunks_changed`/`CHUNK_DATA` bytes as a reference run), a malformed
  `VERDICTS` (count mismatch, short bitmap) raising `ProtocolError`.
- **Step 2:** a **deadlock test** that fills both directions at once — a basis large enough that the
  digest stream exceeds the socket buffer while the sender is writing page data — asserting completion
  under a timeout, run both over `local`'s socketpair and over a real socket. Plus: a digest for an
  out-of-range chunk rejected; a file B lacks; a file B has shorter; a `CHUNK_COPY` run spanning a
  whole unchanged file (assert one frame, not N); a `CHUNK_COPY` and a `CHUNK_DATA` naming the same
  chunk rejected; a chunk named by neither caught by `FILE_DONE`'s digest check.
- **Step 3:** fsyncs completing before the rename (patch `os.fsync` to record order), and the
  send-queue reader degrading to null busy/idle on a socketpair.

Run: `python -m unittest discover -s tests`. CI is `tests.yml` (~30 s) plus `native-baseline.yml`
(~4 min). **Local note:** this project's tests only run properly in Docker on this machine, and
`core.autocrlf` must stay off or the shell scripts break.

## 12. Sequencing and risk

1. Branch off `origin/main` per the repo's one-branch-per-issue rule. Proposed names:
   `pipeline/batched-hashes`, `pipeline/both-sides-hash`, `pipeline/overlap`. The design doc lands as
   the first commit of the first branch.
2. Step 1 → benchmark on the laptop demo → PR citing #36 → merge.
3. Step 2 → laptop demo + one same-region cloud run → PR → merge.
4. Step 3 → gated on a latency session (§9).
5. Step 4's tables accumulate as each step lands, rather than as one final PR.

**Risks**

- **#37 collides.** Parallel connections/workers touches the same three files and is unowned. Whoever
  lands second rebases; flag it the moment someone takes it. Step 2's reader-thread structure is also
  the natural seam for #37's multiple connections, so the two owners should agree on it early.
- **Protocol version.** Bumping to 2 means a v1 receiver cannot talk to a v2 sender. The benchmark
  harness deploys both ends together, so this is acceptable; it must be stated in the PR, and the
  mismatch error must stay legible.
- **The naive baseline must not rot.** `--batch 1` is the regression anchor for G4; if it drifts from
  today's numbers, the comparison in #20 is worthless. One test asserts the equivalence.

## 13. What is left to the builder

- The default for `--batch`: 128 is a placeholder. The 16/128/1024 sweep decides it, and the PR states
  the measured choice.
- The default for `--digest-window` and `--hash-ahead`: measured in steps 2 and 3, not assumed.
- Whether `batches` and `chunks_asked` both belong in the report, or `chunks_asked` alone with
  `round_trips` keeping its name. Either is fine provided published runs stay comparable.
- The exact bitmap bit order (LSB-first is specified above; any consistent choice works if the tests
  pin it).
- Whether the receiver's fsync worker is a thread or a small pool — EXP-92 used a worker; a pool is
  only worth it if the fsyncs serialise.
