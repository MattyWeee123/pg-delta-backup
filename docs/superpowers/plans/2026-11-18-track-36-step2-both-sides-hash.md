# Both sides hash at the same time (track #36, step 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the request/reply dependency entirely — the receiver streams B's digests unasked while the sender hashes S, so no round trip remains per batch or per chunk.

**Architecture:** The receiver gains a generator thread that walks the same file list in the same order, reads and hashes B, and streams `BASIS_DIGESTS` without being asked; its main loop keeps receiving and writing. The sender gains a reader thread that drains those digests into a `(file, chunk) → digest` table while its main thread hashes S, compares locally, and streams `CHUNK_COPY` run-length instructions for unchanged runs plus `CHUNK_DATA` for the rest. This is rsync's generator → sender → receiver pipeline. Both sides therefore have a thread permanently in `recv`, which is what makes the two-way streaming deadlock-free.

**Tech Stack:** Python 3.11+, standard library only (`struct`, `hashlib.blake2b`, `socket`, `threading`, `unittest`). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-18-track-36-pipelining-design.md` §8

## Global Constraints

- Python 3.11 or later; **no new dependencies**.
- `protocol.VERSION` becomes `3`. Sender and receiver are deployed as a pair; a mismatch must still send a legible `ERROR` frame naming both versions (step 1 added that path and its test — keep both working).
- **Entry order is the contract.** The receiver's generator walks `entries` in HELLO order; the sender consumes files in the same order. Neither side may reorder.
- Within a file, chunk indices on the wire stay **monotonically ascending**, and a `CHUNK_COPY` run never spans two files.
- **`CHUNK_COPY` and `CHUNK_DATA` are mutually exclusive per chunk and each chunk is named exactly once.** `FILE_DONE`'s whole-file digest is the backstop that catches a violation; it already exists and must keep passing.
- **Every write to O is positional** (`os.pwrite`), so no two threads share a file offset.
- **`open_file`'s "the previous file is finished" assumption belongs to the main loop only.** The receiver's generator thread keeps its own basis file handles and must never touch `files`.
- The sender may only block waiting for a digest it has a reason to expect: `basis_sizes` from ACCEPT says which files B has and how long they are. A chunk outside that is `chunks_new`, sent without waiting.
- **`EventLog` stays single-writer.** Progress is emitted from the sender's main loop only; `viz` parses the stream as JSON lines and an interleaved write corrupts it.
- Digests per `BASIS_DIGESTS` frame are capped so the frame fits `MAX_PAYLOAD` (`64 << 20`), as step 1 caps `CHUNK_HASHES`.
- `tests/test_pgdelta.py::test_a_wrong_write_is_caught_and_no_output_appears` must stay green and **unmodified**.
- **This engine cannot run on Windows** (`os.pread`/`os.pwrite` absent) and this repo's Pyright diagnostics are unreliable. Verify on Linux: `wsl -d Ubuntu-22.04 -- bash -c 'rm -rf /tmp/pgd && mkdir -p /tmp/pgd && cp -r /mnt/c/Users/samet/pg-delta-backup/pgdelta /mnt/c/Users/samet/pg-delta-backup/tests /mnt/c/Users/samet/pg-delta-backup/bench /mnt/c/Users/samet/pg-delta-backup/viz /tmp/pgd/ && cd /tmp/pgd && python3 -m unittest discover -s tests -p "test_pgdelta*.py" && python3 -m unittest discover -s tests -p "test_bench.py"'`. Known-good at this plan's base: **41 OK** and **17 OK**.
- Branch from `pipeline/batched-hashes` (step 1, PR #43, unmerged). If step 1's protocol changes in review, this work rebases.

## Review Focus

1. **A peer that dies while the sender is waiting for a digest.** The main thread must raise, never hang. The reader thread records the exception and wakes every waiter; a `Closed` or `PeerError` on the reader must surface on the main thread. Test in Task 2.
2. **A `BASIS_DIGESTS` frame naming a file B never declared, or a chunk beyond the size ACCEPT declared.** Must raise `ProtocolError` on the sender rather than silently poisoning the table. Test in Task 2.
3. **A `CHUNK_COPY` run that spans a file boundary, or names a chunk outside B's size or past S's declared size.** Must raise `ProtocolError` on the receiver. Test in Task 2.
4. **A file B has but whose digests never arrive** (a truncated or lying peer). The sender must fail with a legible error rather than block forever at `COMPLETE`. Test in Task 2.
5. **`--digest-window` smaller than one `BASIS_DIGESTS` frame.** ~~The reader would refuse to admit any digest … a self-inflicted deadlock.~~ **That premise was wrong, and Task 3 disproved it empirically.** The window is checked *between* frames and `recv` admits a whole frame atomically, so a one-byte window self-drains frame to frame and never deadlocks. The floor still earns its place — it keeps the knob's units meaningful and avoids stop-start on every frame — but it prevents pathological churn, not a hang. Task 3 tests that a tiny window completes; it must not claim to test deadlock prevention.

---

## File Structure

- `pgdelta/protocol.py` — gains `BASIS_DIGESTS`, `CHUNK_COPY`, their structs, and a validated digest-frame unpacker. Retires `CHUNK_HASHES`/`VERDICTS` and the bitmap codec in Task 2.
- `pgdelta/sender.py` — the reader thread, the digest table with its bounded wait, and run-length `CHUNK_COPY` emission replace the accumulator.
- `pgdelta/receiver.py` — the generator thread; the main loop's `CHUNK_HASHES` handler is replaced by `CHUNK_COPY`.
- `pgdelta/cli.py` — `--digest-window`.
- `pgdelta/README.md`, `README.md`, `bench/README.md`, `viz/README.md`, `bench/report.py`, `bench/gcp.py`, `bench/laptop.py` — the accounting catch-up in Task 4.
- `tests/test_pgdelta_stream.py` — **new**: streaming, deadlock, error propagation, malformed frames.
- `tests/test_pgdelta_batch.py`, `tests/test_pgdelta.py` — migrate the assertions step 1's protocol pinned.

---

### Task 1: The two new messages and a validated digest-frame unpacker

**Files:**
- Modify: `pgdelta/protocol.py`
- Test: `tests/test_pgdelta_stream.py` (create)

**Interfaces:**
- Produces:
  - `BASIS_DIGESTS = 12`, `CHUNK_COPY = 13`, both in `NAMES`
  - `BASIS_DIGESTS_HEAD = struct.Struct('>IQI')` — (file index, first chunk index, count)
  - `CHUNK_COPY_BODY = struct.Struct('>IQI')` — (file index, first chunk index, count)
  - `MAX_DIGESTS_PER_FRAME = (MAX_PAYLOAD - BASIS_DIGESTS_HEAD.size) // DIGEST_SIZE` — evaluate to 4194303
  - `DIGESTS_PER_FRAME = 1024` — how many digests the receiver's generator puts in one frame (16,384
    bytes of digests). It lives here, not in `receiver.py`, because `sender.py` needs it to floor its
    window and must not import the receiver.
  - `unpack_digest_frame(payload: bytes) -> tuple[int, int, list[bytes]]` — returns (file index, first chunk, digests); raises `ProtocolError` for a payload shorter than the head, `count == 0`, a body whose length is not exactly `count * DIGEST_SIZE`, or `count > MAX_DIGESTS_PER_FRAME`. It must validate against the frame's actual length **before** `count` sizes any list or slice.
- `VERSION` stays 2 in this task; Task 2 bumps it to 3 with the rest of the switch.

- [ ] **Step 1: Write the failing tests**

```python
class DigestFrames(unittest.TestCase):
    def test_round_trips_index_first_chunk_and_digests(self):
        digests = [bytes([i]) * protocol.DIGEST_SIZE for i in range(3)]
        payload = protocol.BASIS_DIGESTS_HEAD.pack(7, 100, 3) + b''.join(digests)
        self.assertEqual(protocol.unpack_digest_frame(payload), (7, 100, digests))

    def test_a_payload_shorter_than_the_head_is_refused(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.unpack_digest_frame(b'\x00' * (protocol.BASIS_DIGESTS_HEAD.size - 1))

    def test_a_zero_count_is_refused(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.unpack_digest_frame(protocol.BASIS_DIGESTS_HEAD.pack(0, 0, 0))

    def test_a_body_that_is_not_a_whole_number_of_digests_is_refused(self):
        payload = protocol.BASIS_DIGESTS_HEAD.pack(0, 0, 2) + b'\x00' * (protocol.DIGEST_SIZE + 1)
        with self.assertRaises(protocol.ProtocolError):
            protocol.unpack_digest_frame(payload)

    def test_a_count_beyond_the_body_never_sizes_an_allocation(self):
        payload = protocol.BASIS_DIGESTS_HEAD.pack(0, 0, 2 ** 32 - 1) + b'\x00' * protocol.DIGEST_SIZE
        with self.assertRaises(protocol.ProtocolError):
            protocol.unpack_digest_frame(payload)

    def test_the_frame_cap_matches_max_payload(self):
        self.assertEqual(protocol.MAX_DIGESTS_PER_FRAME, 4194303)
        biggest = protocol.BASIS_DIGESTS_HEAD.size + protocol.MAX_DIGESTS_PER_FRAME * protocol.DIGEST_SIZE
        self.assertLessEqual(biggest, protocol.MAX_PAYLOAD)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_pgdelta_stream -v`
Expected: FAIL — `module 'pgdelta.protocol' has no attribute 'unpack_digest_frame'`

- [ ] **Step 3: Add the constants, structs and unpacker to `pgdelta/protocol.py`**

Follow the module's conventions: module-level `struct.Struct` constants, `ProtocolError` for anything a peer got wrong. Leave `CHUNK_HASHES`, `VERDICTS`, `pack_verdicts` and `unpack_verdicts` in place; Task 2 retires them, which keeps the suite green across this commit.

- [ ] **Step 4: Run the whole suite on Linux**

Expected: the new tests pass; `test_pgdelta*` rises from 41 to 47, `test_bench` stays 17, zero failures.

- [ ] **Step 5: Commit**

```bash
git add pgdelta/protocol.py tests/test_pgdelta_stream.py
git commit -m "protocol: B's digests, unasked, and a copy instruction (#36, #18)"
```

---

### Task 2: Both sides stream

The atomic task: a receiver that volunteers digests breaks a sender that expects `VERDICTS`, so both sides, the retirement of step 1's messages, and the migrated assertions land in one commit.

**Files:**
- Modify: `pgdelta/sender.py`, `pgdelta/receiver.py`, `pgdelta/protocol.py`, `pgdelta/cli.py`
- Modify: `tests/test_pgdelta_batch.py`, `tests/test_pgdelta.py`
- Test: `tests/test_pgdelta_stream.py`

**Interfaces:**
- Consumes: Task 1's `BASIS_DIGESTS`, `CHUNK_COPY`, `BASIS_DIGESTS_HEAD`, `CHUNK_COPY_BODY`, `unpack_digest_frame`, `MAX_DIGESTS_PER_FRAME`
- Produces:
  - `protocol.VERSION = 3`
  - `pgdelta.sender.DEFAULT_DIGEST_WINDOW = 64 << 20` — bytes of undelivered digests the reader thread will hold (4,194,304 digests, about 32 GiB of basis at an 8 KiB chunk)
  - `run_sender(channel, source, chunk_size=DEFAULT_CHUNK_SIZE, events=None, digest_window=DEFAULT_DIGEST_WINDOW) -> dict` — `batch`/`batch_bytes` are **gone**; nothing is asked about any more
  - report `counts`: `round_trips` counts the times this side blocked on the peer (ACCEPT, each digest wait, RESULT); `chunks_asked` keeps its name and now means chunks compared against a B digest; `chunks_same`, `chunks_changed`, `chunks_new`, `files`, `bytes_read` unchanged; new `digest_waits` and `copy_runs`
  - report `seconds`: `wait_for_verdict` becomes `wait_for_digests`
- `--batch` and `--batch-bytes` are removed from `cli.py`; `--digest-window` arrives in Task 3, so this task threads `digest_window` with its default only.

- [ ] **Step 1: Write the failing tests**

In `tests/test_pgdelta_stream.py`, reusing `RefreshFixture` from `test_pgdelta` (subclass `RefreshFixture, unittest.TestCase` — never `Refresh`, which would re-collect its tests):

```python
class Streaming(RefreshFixture, unittest.TestCase):
    def test_no_chunk_is_asked_about(self):
        self.both('base/1/16384', os.urandom(5 * CHUNK))
        code, report = self.refresh()
        self.assertEqual(code, 0)
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
        self.assertNotIn('CHUNK_HASHES', report['bytes_sent'])
        self.assertEqual(report['counts']['chunks_same'], 5)

    def test_an_unchanged_file_costs_one_copy_run(self):
        self.both('f', os.urandom(40 * CHUNK))
        code, report = self.refresh()
        self.assertEqual(report['counts']['copy_runs'], 1)
        self.assertEqual(report['counts']['chunks_same'], 40)
        self.assertNotIn('CHUNK_DATA', report['bytes_sent'])

    def test_copy_runs_break_around_changed_chunks(self):
        data = bytearray(os.urandom(10 * CHUNK))
        write(self.b, 'f', bytes(data))
        for i in (3, 7):
            data[i * CHUNK] ^= 0xFF
        write(self.s, 'f', bytes(data))
        code, report = self.refresh()
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
        self.assertEqual(report['counts']['chunks_changed'], 2)
        self.assertEqual(report['counts']['copy_runs'], 3)   # 0-2, 4-6, 8-9

    def test_a_file_b_lacks_waits_for_nothing(self):
        write(self.s, 'new', os.urandom(3 * CHUNK))
        self.both('kept', os.urandom(CHUNK))
        code, report = self.refresh()
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
        self.assertEqual(report['counts']['chunks_new'], 3)

    def test_a_shorter_basis_file_waits_only_for_the_range_b_has(self):
        base = os.urandom(2 * CHUNK)
        write(self.b, 'grew', base)
        write(self.s, 'grew', base + os.urandom(2 * CHUNK))
        code, report = self.refresh()
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
        self.assertEqual(report['counts']['chunks_same'], 2)
        self.assertEqual(report['counts']['chunks_new'], 2)

    def test_round_trips_no_longer_scale_with_chunks(self):
        """The headline: 200 chunks, and the sender blocks a handful of times, not 200."""
        self.both('f', os.urandom(200 * CHUNK))
        code, report = self.refresh()
        self.assertLess(report['counts']['round_trips'], 10)
```

The deadlock test, which is the one that matters — run it over `local` (a `socketpair`, whose buffers are tens of KB, so it deadlocks sooner) **and** over a real socket, with a timeout so a hang fails instead of hanging the suite:

```python
def run_with_timeout(case, refresh, seconds, *extra):
    """Run `refresh(*extra)` on a thread and fail if it does not finish. A deadlocked engine shows up
    as a thread that never returns, so the assertion is on the thread, not on an exception."""
    outcome = {}

    def go():
        try:
            outcome['code'] = refresh(*extra)[0]
        except BaseException as exc:
            outcome['error'] = exc

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    thread.join(timeout=seconds)
    case.assertFalse(thread.is_alive(), 'the refresh never finished: both sides are deadlocked')
    case.assertNotIn('error', outcome, f"refresh raised: {outcome.get('error')!r}")
    case.assertEqual(outcome['code'], 0)


class Deadlock(RefreshFixture, unittest.TestCase):
    """A basis big enough that the digest stream exceeds the socket buffer while the sender is still
    writing page data. A deadlocked engine fails these by timeout, not by assertion, so each runs on
    a thread and the assertion is that the thread finished."""

    def big_pair(self, chunks=3000, every=4):
        data = bytearray(os.urandom(chunks * CHUNK))
        write(self.b, 'f', bytes(data))
        for c in range(0, chunks, every):
            data[c * CHUNK] ^= 0xFF
        write(self.s, 'f', bytes(data))

    # run_with_timeout is a MODULE-LEVEL helper in this file, not a method: Task 3's tiny-window
    # test needs the same thing, and a deadlock can only ever be detected as a timeout.

    def test_local_socketpair_completes_with_both_directions_busy(self):
        """`local` is the harsher case: socketpair buffers are tens of KB, not megabytes."""
        self.big_pair()
        run_with_timeout(self, self.refresh, 120)
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))

    def test_a_real_socket_completes_with_both_directions_busy(self):
        # same corpus over run_sender/run_receiver on a 127.0.0.1 socket, as OverTcp does
```

And the failure paths from Review Focus:

```python
class StreamFailures(unittest.TestCase):
    """Each drives one real side over a socketpair and feeds it a hostile peer, as step 1's
    MalformedBatch does. The assertion is always: a legible error, within a timeout, and no O."""

    def test_a_dead_peer_raises_instead_of_hanging(self):
        """Review Focus 1 — the failure that would otherwise hang a user's refresh forever."""
        # stand in as the receiver: read HELLO, send ACCEPT declaring a basis for the one file,
        # then close the socket without sending any BASIS_DIGESTS.
        # run_sender on a thread; assert the thread finishes within 30 s and raised
        # protocol.Closed or protocol.PeerError -- NOT that it returned.

    def test_a_digest_for_an_undeclared_file_is_refused(self):
        # ACCEPT declares nothing, then a BASIS_DIGESTS naming index 0
        # -> ProtocolError mentioning the index

    def test_a_digest_past_the_declared_basis_size_is_refused(self):
        # ACCEPT declares 2 chunks, then a BASIS_DIGESTS whose run reaches chunk 5
        # -> ProtocolError mentioning "outside"

    def test_missing_digests_for_a_file_b_has_fail_legibly(self):
        """Review Focus 4 — digests for only half the range B declared."""
        # assert the sender raises within a timeout, and the message names the file

    def test_a_copy_run_spanning_two_files_is_refused(self):
        # drive run_receiver; a CHUNK_COPY whose run reaches past file 0's chunk count
        # -> ProtocolError, ERROR frame to the peer, no O, no staging directory left

    def test_a_copy_run_outside_bs_size_is_refused(self):
        # B truncated to 2 chunks, S declares 4, CHUNK_COPY names chunks 0-3 -> ProtocolError
```

- [ ] **Step 2: Run them to verify they fail**

Expected: FAIL — `KeyError: 'copy_runs'` and `ProtocolError: expected VERDICTS, got BASIS_DIGESTS`.

- [ ] **Step 3: Give the receiver a generator thread (`pgdelta/receiver.py`)**

A thread started after ACCEPT that walks `entries` in order and, for every file in `basis_size`, reads B in `chunk_size` pieces, hashes each, and sends `BASIS_DIGESTS` every `protocol.DIGESTS_PER_FRAME` digests and at each file's end. It **opens its own basis file handles and never touches `files`** — `open_file`'s "previous file is finished" invariant belongs to the main loop. Only this thread writes to the channel in the receiver's direction, so the single-writer rule holds. Record any exception and close the channel so the main loop does not wait forever; the existing `except BaseException` handler must still remove the staging directory exactly once.

- [ ] **Step 4: Replace the receiver's `CHUNK_HASHES` handler with `CHUNK_COPY`**

`CHUNK_COPY` validates `count != 0`, that `first_chunk + count - 1` is within the file's declared size via `span`, and that the whole run lies inside `basis_size[index]`; then, for each chunk in the run, `pread` from B and `pwrite` into O, counting `chunks_same`. Reject a run naming a file B lacks or already in `done`.

- [ ] **Step 5: Give the sender a reader thread and a digest table (`pgdelta/sender.py`)**

A thread that loops on `channel.recv()`, accepts only `BASIS_DIGESTS` (anything else is a `ProtocolError`), validates each frame with `unpack_digest_frame`, checks the frame against `basis_sizes` (unknown index, or a chunk beyond the declared size, is a `ProtocolError`), and stores digests in a dict keyed `(index, chunk)` under a `threading.Condition`. It stops admitting once the table holds `digest_window` bytes of undelivered digests, which lets TCP backpressure stall the receiver's generator. On any exception, or on `Closed`, it stores the exception and `notify_all()` so a waiting main thread raises rather than hangs.

A helper the main thread uses: `_await_digest(index: int, chunk: int) -> bytes`, which returns a stored digest, waiting on the condition if it has not arrived, re-raising the reader's stored exception if one is set, and counting one `digest_waits` and one `round_trips` plus `wait_for_digests` time whenever it actually blocks. A digest consumed is removed from the table so the window drains.

- [ ] **Step 6: Replace the sender's accumulator with compare-and-emit**

For each chunk: read, feed the whole-file digest, and if `basis_size is not None and offset + want <= basis_size` then hash it and compare against `_await_digest`; otherwise count `chunks_new`. Accumulate consecutive equal chunks into a run and emit one `CHUNK_COPY` when the run ends — at a differing chunk, at a `chunks_new` chunk, or at the file's end — counting `copy_runs`. A differing chunk sends `CHUNK_DATA` immediately. **Flush the pending copy run before any `CHUNK_DATA` or `FILE_DONE`** so chunk indices stay ascending and runs never span files. Remove `batch`, `batch_bytes`, `effective_batch` and `_MAX_BATCH_FOR_PAYLOAD`, and the `--batch`/`--batch-bytes` options from `cli.py`.

- [ ] **Step 7: Retire step 1's messages**

Delete `CHUNK_HASHES`, `VERDICTS`, `CHUNK_HASHES_HEAD`, `VERDICTS_HEAD`, `pack_verdicts`, `unpack_verdicts` and their `NAMES` entries from `pgdelta/protocol.py`, with their imports. Bump `VERSION` to 3, keeping the legible-mismatch path and its test.

- [ ] **Step 8: Migrate the assertions step 1's protocol pinned**

In `tests/test_pgdelta_batch.py`, the `Verdicts`, `BatchedExchange`, `MalformedBatch` and `BatchOptions` classes test a protocol that no longer exists. Delete the file and move anything still meaningful into `tests/test_pgdelta_stream.py` — the boundary cases worth keeping are the straddling partial chunk, the grown and shrunk files, the empty file, and no-output-published-after-a-bad-frame. In `tests/test_pgdelta.py`, `test_identical_copies_send_no_chunk` asserts `round_trips == 1` and `chunks_asked == 5`: keep `chunks_asked`, and assert `copy_runs == 1`. Leave `test_a_wrong_write_is_caught_and_no_output_appears` untouched.

- [ ] **Step 9: Run the whole suite on Linux and commit**

Expected: zero failures. Report the counts.

```bash
git add pgdelta tests
git commit -m "Both sides hash at the same time (#36, #18)"
```

---

### Task 3: `--digest-window`, and the floor that keeps it out of frame-at-a-time churn

**Files:**
- Modify: `pgdelta/cli.py`, `pgdelta/sender.py`
- Test: `tests/test_pgdelta_stream.py`

**Interfaces:**
- Consumes: Task 2's `run_sender(..., digest_window)` and `DEFAULT_DIGEST_WINDOW`
- Produces: `sender.MIN_DIGEST_WINDOW = protocol.DIGESTS_PER_FRAME * protocol.DIGEST_SIZE` — evaluate to 16384; `sender.effective_digest_window(digest_window: int) -> int`; `--digest-window BYTES` on `local` and `send`

- [ ] **Step 1: Write the failing tests**

```python
class DigestWindow(RefreshFixture, unittest.TestCase):
    def test_the_window_floors_at_one_whole_frame(self):
        self.assertEqual(sender.MIN_DIGEST_WINDOW, 16384)
        self.assertEqual(sender.effective_digest_window(1), sender.MIN_DIGEST_WINDOW)
        self.assertEqual(sender.effective_digest_window(64 << 20), 64 << 20)

    def test_a_tiny_window_still_completes(self):
        """Review Focus 5: a window below one frame starts the reader already over its limit on the
        first frame, collapsing the pipeline into frame-at-a-time handoff. It does NOT deadlock --
        see the corrected Review Focus 5 above and MIN_DIGEST_WINDOW's comment in sender.py."""
        # 300 chunks of basis; run_with_timeout(self, self.refresh, 60, '--digest-window', '1')
        # then assert tree_bytes(self.o) == tree_bytes(self.s)

    def test_a_non_positive_window_is_a_bad_argument(self):
        for value in ('0', '-1'):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                self.refresh('--digest-window', value)
            self.assertEqual(caught.exception.code, 2)
```

- [ ] **Step 2: Run them to verify they fail**

Expected: FAIL — `module 'pgdelta.sender' has no attribute 'MIN_DIGEST_WINDOW'`.

- [ ] **Step 3: Implement `effective_digest_window(digest_window: int) -> int` in `pgdelta/sender.py`**

`max(MIN_DIGEST_WINDOW, digest_window)`. Call it once in `run_sender` beside the existing chunk-size validation, and raise `ValueError` for `digest_window < 1` so a direct library call is also guarded.

- [ ] **Step 4: Add `--digest-window` in `pgdelta/cli.py`**

On `local` and `send`, `type=positive_int` (step 1 added that callable), `default=DEFAULT_DIGEST_WINDOW`, with help text saying it bounds the undelivered digests the sender holds and that the floor is one frame. A bad value must exit **2** via argparse, not 1.

- [ ] **Step 5: Run the whole suite on Linux and commit**

```bash
git add pgdelta/cli.py pgdelta/sender.py tests/test_pgdelta_stream.py
git commit -m "--digest-window, floored at one frame so it cannot starve itself (#36, #18)"
```

---

### Task 4: The accounting and the documentation catch up

Step 1 taught this lesson at the cost of a whole-branch review finding: when a counter changes meaning, the prose and the benchmark table are part of the change, not a follow-up.

**Files:**
- Modify: `pgdelta/README.md`, `README.md`, `bench/README.md`, `viz/README.md`, `bench/report.py`, `bench/gcp.py`, `bench/laptop.py`, `pgdelta/cli.py`
- Test: `tests/test_bench.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_bench.py`, extend the report test: a facts dict carrying `copy_runs` renders it, and one without (an older run) still renders without crashing or printing a misleading number.

- [ ] **Step 2: Carry `copy_runs` through `bench/`**

`bench/gcp.py` and `bench/laptop.py` persist `copy_runs` beside `round_trips` and `chunks_asked`, read defensively (`c.get('copy_runs', 0)`); `bench/report.py` shows it, keeping `–` for a lane with no such counter and the existing footnote accurate for runs recorded before this change.

- [ ] **Step 3: Update the prose, with no unmeasured magnitude anywhere**

- `pgdelta/README.md`: the protocol diagram becomes the streamed exchange (`BASIS_DIGESTS` unasked, `CHUNK_COPY` runs, `CHUNK_DATA`); the "why it's naive" table loses the per-batch wait row and gains what remains; `wait_for_verdict` becomes `wait_for_digests`; `copy_runs` and `digest_waits` join the counts list.
- `README.md`: "How the naive version works" describes the streamed exchange. **The sample trace stays numerically untouched** — it cannot be regenerated here — and its existing annotation is extended to say the round-trip figure predates both batching and streaming.
- `bench/README.md`: the column list gains Copy runs.
- `viz/README.md`: the progress-event field list and the stale-figure note stay accurate.
- `pgdelta/cli.py`'s `summary()`: "N round trips asking about M chunks" no longer describes the engine. State what the sender actually did — chunks compared, copy runs, chunks sent — and how many times it had to wait.

**State no factor and no speedup.** Round trips fall to a handful, and that is observable from the counters; the time it buys is not measured until a run on two machines with a real link.

- [ ] **Step 4: Run the whole suite on Linux and commit**

```bash
git add pgdelta bench README.md viz/README.md tests/test_bench.py
git commit -m "Streaming accounting: copy runs, digest waits, and the prose that explains them (#36, #18)"
```

- [ ] **Step 5: Open the pull request**

Against `main` once step 1 has merged (this branch is stacked on it), filling the template's four sections. **Evidence must be the counters and the deadlock test, not a time claim.** Limitations: `--digest-window` is unmeasured, so its default is provisional exactly as `--batch`'s was; the sweep workflow from step 1 can measure it once dispatchable.
