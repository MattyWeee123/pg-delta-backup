# Batched chunk digests (track #36, step 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the one-round-trip-per-8-KiB-chunk exchange with a batched one — N digests per question, answered by a bitmap — so round trips fall from one per chunk to `ceil(chunks/N)`.

**Architecture:** Two new message types (`CHUNK_HASHES`, `VERDICTS`) retire `CHUNK_HASH`/`VERDICT`; `protocol.VERSION` goes to 2. The sender accumulates digests for contiguous chunks of one file that lie wholly within B's size, flushes on a count limit, a byte limit, a file end, or a chunk B lacks, then sends `CHUNK_DATA` for each set bit. The receiver answers one batch with one bitmap. `--batch 1` reproduces stop-and-wait semantics from the same binary, which is the baseline step #20 compares against.

**Tech Stack:** Python 3.11+, standard library only (`struct`, `hashlib.blake2b`, `socket`, `unittest`). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-18-track-36-pipelining-design.md`

## Global Constraints

- Python 3.11 or later; **no new dependencies** (the engine is stdlib-only today and stays so).
- `protocol.VERSION` becomes `2`. A version mismatch must still raise `ProtocolError` with a legible message; sender and receiver are deployed as a pair.
- **Files are never reordered on the wire.** `receiver.py`'s `open_file` closes the previous file when a new index appears, on the comment "files arrive in order: the previous one is finished".
- Within a file, chunk indices on the wire stay **monotonically ascending**.
- Only chunks lying wholly within B's size for that file may be asked about. The existing predicate is `basis_size is not None and offset + want <= basis_size`; chunks failing it are `chunks_new` and are sent unasked.
- `--chunk-size` stays independent of batch size: valid range 512 to `16 << 20`, default 8192.
- Batch memory is bounded in **bytes**, not count: `--batch-bytes` default `32M`, effective batch `min(--batch, --batch-bytes // chunk_size, (MAX_PAYLOAD - CHUNK_HASHES_HEAD.size) // DIGEST_SIZE)`, minimum 1. The third cap keeps a user-set `--batch-bytes` from building a frame the channel refuses (ruling R8).
- `MAX_PAYLOAD` is `64 << 20`; a frame larger than it must be rejected **before** any allocation sized by a peer-supplied count.
- Digest size comes from `pgdelta.hashing.DIGEST_SIZE` (16), imported rather than hardcoded in new structs.
- `stats.py` must keep emitting `bytes_source_to_dest` and `bytes_dest_to_source` (read by `benchmarks/methods.py`) and `counts['round_trips']` (read by `viz` progress events and `cli.summary`).
- `test_a_wrong_write_is_caught_and_no_output_appears` must stay green **unmodified** at every task.
- Tests: `python -m unittest discover -s tests`. On this machine the suite runs in Docker, and `core.autocrlf` must stay off.

## Review Focus

1. **A peer-supplied `count` that is huge or zero.** A `CHUNK_HASHES` head claiming `count = 2^32-1` must be rejected from the frame length alone, before any list or buffer is sized by it; `count = 0` must be rejected too, not answered with an empty bitmap. Test in Task 2.
2. **A `VERDICTS` body that disagrees with the batch just sent.** A short bitmap, a long one, or an echoed count that differs must raise `ProtocolError` rather than silently leaving chunks unwritten — an unwritten chunk would otherwise surface only as a `FILE_DONE` digest failure. Test in Task 2.
3. **A chunk only partially covered by B.** When `basis_size` is not a multiple of `chunk_size`, the final chunk inside B's range is partial and must be treated as `chunks_new`, never batched. Test in Task 2.
4. **`--chunk-size 16M` with `--batch 1024`.** The byte cap must bind and hold the batch to the two chunks a 32 MiB budget allows, or the sender buffers 16 GiB. Test in Task 3.
5. **`--batch 0` or a negative value.** Must exit 2 as a bad argument, not hang or send an empty frame. Test in Task 3.

---

## File Structure

- `pgdelta/protocol.py` — gains `CHUNK_HASHES`, `VERDICTS`, their structs, and the bitmap codec. Owns the wire format, so the codec lives here.
- `pgdelta/sender.py` — the batch accumulator replaces the per-chunk block.
- `pgdelta/receiver.py` — one new handler replaces the `CHUNK_HASH` handler.
- `pgdelta/cli.py` — `--batch` and `--batch-bytes` on `local` and `send`.
- `pgdelta/README.md` — the protocol diagram and the "why it's naive" table.
- `tests/test_pgdelta.py` — migrate the assertions that change by design.
- `tests/test_pgdelta_batch.py` — **new**: batch boundaries, malformed frames, equivalence.

---

### Task 1: The wire format and the bitmap codec

**Files:**
- Modify: `pgdelta/protocol.py`
- Test: `tests/test_pgdelta_batch.py` (create)

**Interfaces:**
- Consumes: `pgdelta.hashing.DIGEST_SIZE`
- Produces:
  - `CHUNK_HASHES = 10`, `VERDICTS = 11`, both added to `NAMES`
  - `CHUNK_HASHES_HEAD = struct.Struct('>IQI')` — (file index, first chunk index, count)
  - `VERDICTS_HEAD = struct.Struct('>I')` — (count)
  - `pack_verdicts(differs: list[bool]) -> bytes` — head + bitmap, LSB-first within each byte
  - `unpack_verdicts(payload: bytes, expected: int) -> list[bool]` — raises `ProtocolError` if the echoed count differs from `expected` or the bitmap length is not `ceil(expected/8)`
  - `VERSION = 2`

- [ ] **Step 1: Write the failing tests**

```python
class Verdicts(unittest.TestCase):
    def test_bitmap_round_trips_lsb_first(self):
        for flags in ([True], [False], [False] * 8, [True] * 8,
                      [True, False, False, False, False, False, False, False, True]):
            payload = protocol.pack_verdicts(flags)
            self.assertEqual(protocol.unpack_verdicts(payload, len(flags)), flags)

    def test_one_byte_per_eight_chunks(self):
        self.assertEqual(len(protocol.pack_verdicts([False] * 9)),
                         protocol.VERDICTS_HEAD.size + 2)

    def test_first_chunk_is_the_low_bit(self):
        payload = protocol.pack_verdicts([True] + [False] * 7)
        self.assertEqual(payload[protocol.VERDICTS_HEAD.size], 0x01)

    def test_a_count_mismatch_is_refused(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.unpack_verdicts(protocol.pack_verdicts([True, False]), 3)

    def test_a_short_bitmap_is_refused(self):
        payload = protocol.pack_verdicts([False] * 9)
        with self.assertRaises(protocol.ProtocolError):
            protocol.unpack_verdicts(payload[:-1], 9)

    def test_the_protocol_version_is_two(self):
        self.assertEqual(protocol.VERSION, 2)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_pgdelta_batch -v`
Expected: FAIL — `module 'pgdelta.protocol' has no attribute 'pack_verdicts'`

- [ ] **Step 3: Add the constants, structs and codec to `pgdelta/protocol.py`**

Add `CHUNK_HASHES = 10` and `VERDICTS = 11` with their `NAMES` entries, the two structs above, and the two codec functions. Bump `VERSION` to 2. Leave `CHUNK_HASH`, `VERDICT`, `CHUNK_HASH_BODY`, `VERDICT_BODY`, `SAME` and `DIFFERENT` in place for now; Task 2 removes them, which keeps the suite green between commits.

- [ ] **Step 4: Run the whole suite**

Run: `python -m unittest discover -s tests`
Expected: PASS — the new tests pass and nothing else has changed behaviour.

- [ ] **Step 5: Commit**

```bash
git add pgdelta/protocol.py tests/test_pgdelta_batch.py
git commit -m "protocol: a batched digest question and a bitmap answer (#36)"
```

---

### Task 2: The batched exchange on both sides

The smallest atomic unit: the receiver cannot speak the new protocol while the sender speaks the old one, so both sides and the migrated assertions land together.

**Files:**
- Modify: `pgdelta/sender.py`, `pgdelta/receiver.py`
- Modify: `pgdelta/cli.py` (the `--batch` option only; Task 3 adds `--batch-bytes` and the validation)
- Modify: `tests/test_pgdelta.py` (the assertions that change by design)
- Test: `tests/test_pgdelta_batch.py`

**Interfaces:**
- Consumes: Task 1's `CHUNK_HASHES`, `VERDICTS`, `CHUNK_HASHES_HEAD`, `VERDICTS_HEAD`, `pack_verdicts`, `unpack_verdicts`
- Produces:
  - `pgdelta.sender.DEFAULT_BATCH = 128`
  - `run_sender(channel, source, chunk_size=DEFAULT_CHUNK_SIZE, events=None, batch=DEFAULT_BATCH) -> dict`
  - `--batch N` on `local` and `send`, passed through to `run_sender`
  - report `counts` gains `chunks_asked` (chunks asked about — what `round_trips` used to count); `round_trips` now counts batches; `chunks_same`, `chunks_changed`, `chunks_new`, `files`, `bytes_read` keep their meanings
  - report gains top-level `batch` alongside `chunk_size`

- [ ] **Step 1: Write the failing tests**

In `tests/test_pgdelta_batch.py`, reusing `test_pgdelta.py`'s `Refresh` fixture style (`cli.main(['local', ...])` with `--stats-json`):

```python
def test_one_batch_answers_many_chunks(self):
    self.both('base/1/16384', os.urandom(5 * CHUNK))
    code, report = self.refresh('--batch', '128')
    self.assertEqual(code, 0)
    self.assertEqual(report['counts']['chunks_asked'], 5)
    self.assertEqual(report['counts']['round_trips'], 1)
    self.assertEqual(report['counts']['chunks_same'], 5)

def test_batches_do_not_span_two_files(self):
    self.both('a', os.urandom(3 * CHUNK))
    self.both('b', os.urandom(3 * CHUNK))
    code, report = self.refresh('--batch', '128')
    self.assertEqual(report['counts']['chunks_asked'], 6)
    self.assertEqual(report['counts']['round_trips'], 2)

def test_a_batch_stops_at_the_basis_end(self):
    base = os.urandom(3 * CHUNK)
    write(self.b, 'grew', base)
    write(self.s, 'grew', base + os.urandom(2 * CHUNK))
    code, report = self.refresh('--batch', '128')
    self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
    self.assertEqual(report['counts']['chunks_asked'], 3)
    self.assertEqual(report['counts']['chunks_new'], 2)
    self.assertEqual(report['counts']['round_trips'], 1)

def test_a_partly_covered_chunk_is_new_not_asked(self):
    """B's size is not a multiple of the chunk size: the straddling chunk is sent, not asked about."""
    base = os.urandom(2 * CHUNK + 100)
    write(self.b, 'f', base)
    write(self.s, 'f', base + os.urandom(CHUNK))
    code, report = self.refresh('--batch', '128')
    self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
    self.assertEqual(report['counts']['chunks_asked'], 2)
    self.assertEqual(report['counts']['chunks_new'], 1)

def test_a_short_final_batch(self):
    self.both('f', os.urandom(5 * CHUNK))
    code, report = self.refresh('--batch', '2')
    self.assertEqual(report['counts']['chunks_asked'], 5)
    self.assertEqual(report['counts']['round_trips'], 3)

def test_an_empty_file_asks_nothing(self):
    write(self.s, 'empty', b'')
    write(self.b, 'empty', b'')
    code, report = self.refresh('--batch', '128')
    self.assertEqual(code, 0)
    self.assertEqual(report['counts'].get('round_trips', 0), 0)

def test_changed_chunks_in_a_batch_are_sent_in_order(self):
    data = bytearray(os.urandom(6 * CHUNK))
    write(self.b, 'f', bytes(data))
    for i in (1, 4):
        data[i * CHUNK] ^= 0xFF
    write(self.s, 'f', bytes(data))
    code, report = self.refresh('--batch', '128')
    self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
    self.assertEqual(report['counts']['chunks_changed'], 2)
    self.assertEqual(report['counts']['chunks_same'], 4)
    self.assertEqual(report['counts']['round_trips'], 1)
```

And the malformed-frame tests. These drive the receiver directly: run `run_receiver` on a thread over a `socketpair` (as `OverTcp` does), send a valid `HELLO` for a one-file tree, read `ACCEPT`, then inject one bad `CHUNK_HASHES` and assert the receiver answers with an `ERROR` frame and leaves no output.

```python
class MalformedBatch(unittest.TestCase):
    """A peer-supplied count must never size an allocation before it is validated."""

    def inject(self, head, body) -> str:
        """HELLO a single 4-chunk file B also has, then send one CHUNK_HASHES; return the ERROR message."""
        # ... fixture as described above; returns json.loads(payload)['message']

    def test_a_zero_count_is_refused(self):
        head = protocol.CHUNK_HASHES_HEAD.pack(0, 0, 0)
        self.assertIn('count', self.inject(head, b''))

    def test_a_count_beyond_the_body_is_refused(self):
        head = protocol.CHUNK_HASHES_HEAD.pack(0, 0, 2 ** 32 - 1)
        self.assertIn('count', self.inject(head, b'\x00' * protocol.DIGEST_SIZE))

    def test_a_body_that_is_not_a_whole_number_of_digests_is_refused(self):
        head = protocol.CHUNK_HASHES_HEAD.pack(0, 0, 2)
        self.assertIn('count', self.inject(head, b'\x00' * (protocol.DIGEST_SIZE + 1)))

    def test_a_chunk_past_the_file_end_is_refused(self):
        head = protocol.CHUNK_HASHES_HEAD.pack(0, 99, 1)
        self.assertIn('past the end', self.inject(head, b'\x00' * protocol.DIGEST_SIZE))

    def test_no_output_is_published_after_a_bad_batch(self):
        self.test_a_zero_count_is_refused()
        self.assertFalse(self.o.exists())
        self.assertFalse(any(p.name.endswith('.pgdelta-partial') for p in self.o.parent.iterdir()))
```

A chunk outside B's size cannot be injected through this fixture without also lying in `HELLO`, so the receiver's "asked about a file B lacks" guard is covered by `test_a_chunk_past_the_file_end_is_refused` plus the existing `ProtocolError` path; do not add a test that asserts an unreachable branch.

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_pgdelta_batch -v`
Expected: FAIL — `unrecognized arguments: --batch` from `cli.main`, and `ProtocolError: unexpected CHUNK_HASHES` from the receiver tests.

- [ ] **Step 3: Rewrite the sender's inner loop in `pgdelta/sender.py`**

Replace the per-chunk ask/wait block. Accumulate `(chunk_index, digest, data)` for chunks passing the within-B predicate; flush on the count limit, file end, or a chunk B lacks (flush **before** sending that chunk's `CHUNK_DATA`, so chunk indices stay ascending). Flush = `send(CHUNK_HASHES, head + b''.join(digests))`, `unpack_verdicts(channel.expect(VERDICTS), len(batch))`, then `send(CHUNK_DATA, ...)` for each set bit in ascending order. Count `round_trips` per flush and `chunks_asked` per digest. Keep `stats.timed('hash')`, `'read'`, `'send'` and `'wait_for_verdict'` around the same operations as today so the published runs stay comparable; `wait_for_verdict` now wraps the `VERDICTS` wait.

Thread `batch` through: `run_sender`'s new parameter, and a `--batch` option on `local` and `send` in `pgdelta/cli.py` (`type=int`, `default=DEFAULT_BATCH`). The byte cap arrives in Task 3; this task's batches are bounded by count alone, which is safe at the default chunk size and is what Task 3's Review Focus item 4 then closes.

- [ ] **Step 4: Replace the receiver's handler in `pgdelta/receiver.py`**

Handle `CHUNK_HASHES`: validate `count` against the body length **first**, then `open_file(index)`, then for each chunk compute `span`, `pread` B's range, hash, compare, `pwrite` B's bytes into O for the matches (as the `SAME` path does today), and collect the flags. Reply once with `pack_verdicts`. Raise `ProtocolError` for `count == 0`, a body length that is not `count * DIGEST_SIZE`, a file index already in `done`, a file B lacks, and any chunk past the file end or outside B's size.

- [ ] **Step 5: Remove the retired messages**

Delete `CHUNK_HASH`, `VERDICT`, `CHUNK_HASH_BODY`, `VERDICT_BODY`, `SAME`, `DIFFERENT` and their `NAMES` entries from `pgdelta/protocol.py`, and their imports from both sides. Two code paths for the same question is the divergence the spec rules out.

- [ ] **Step 6: Migrate the assertions that change by design in `tests/test_pgdelta.py`**

- `test_identical_copies_send_no_chunk`: `round_trips == 5` becomes `chunks_asked == 5` and `round_trips == 1` at the default batch. Keep `assertNotIn('CHUNK_DATA', report['bytes_sent'])`.
- `test_one_changed_page_is_the_only_chunk_sent`: unchanged — `CHUNK_DATA` framing is untouched.
- `test_new_files_grown_and_shrunk_files`: `chunks_new == 3 + 2` unchanged.
- `test_send_and_receive_on_localhost`: unchanged.
- Leave `test_a_wrong_write_is_caught_and_no_output_appears` alone.

- [ ] **Step 7: Run the whole suite**

Run: `python -m unittest discover -s tests`
Expected: PASS, all tests.

- [ ] **Step 8: Commit**

```bash
git add pgdelta/sender.py pgdelta/receiver.py pgdelta/protocol.py tests/test_pgdelta.py tests/test_pgdelta_batch.py
git commit -m "Ask about many chunks per message instead of one (#36, #17)"
```

---

### Task 3: The option surface and the memory bound

**Files:**
- Modify: `pgdelta/cli.py`, `pgdelta/sender.py`
- Test: `tests/test_pgdelta_batch.py`

**Interfaces:**
- Consumes: Task 2's `run_sender(..., batch)` and its `--batch` option
- Produces: `sender.DEFAULT_BATCH_BYTES = 32 << 20`; `sender.effective_batch(batch: int, batch_bytes: int, chunk_size: int) -> int`; `run_sender`'s further `batch_bytes=DEFAULT_BATCH_BYTES` parameter; `--batch-bytes BYTES` on `local` and `send`

- [ ] **Step 1: Write the failing tests**

```python
class BatchOptions(unittest.TestCase):
    def test_the_byte_cap_binds_before_the_count(self):
        self.assertEqual(sender.effective_batch(1024, 32 << 20, 16 << 20), 2)  # (32<<20)//(16<<20)
        self.assertEqual(sender.effective_batch(1024, 32 << 20, 8192), 1024)
        self.assertEqual(sender.effective_batch(1024, 8192 * 4, 8192), 4)

    def test_the_batch_is_never_zero(self):
        self.assertEqual(sender.effective_batch(1024, 1, 16 << 20), 1)

    def test_a_non_positive_batch_is_a_bad_argument(self):
        """argparse exits 2 from parse_args, which runs before cli.main's try block,
        so SystemExit propagates out of main rather than being returned."""
        for value in ('0', '-1'):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                self.refresh('--batch', value)
            self.assertEqual(caught.exception.code, 2)

    def test_batch_one_matches_the_stop_and_wait_accounting(self):
        """G4's anchor: --batch 1 must reproduce the naive counts chunk for chunk."""
        data = bytearray(os.urandom(6 * CHUNK))
        write(self.b, 'f', bytes(data))
        data[2 * CHUNK] ^= 0xFF
        write(self.s, 'f', bytes(data))
        code, report = self.refresh('--batch', '1')
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
        self.assertEqual(report['counts']['round_trips'], 6)
        self.assertEqual(report['counts']['chunks_asked'], 6)
        self.assertEqual(report['counts']['chunks_same'], 5)
        self.assertEqual(report['counts']['chunks_changed'], 1)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_pgdelta_batch -v`
Expected: FAIL — `module 'pgdelta.sender' has no attribute 'effective_batch'`

- [ ] **Step 3: Implement `effective_batch(batch: int, batch_bytes: int, chunk_size: int) -> int` in `pgdelta/sender.py`**

`max(1, min(batch, batch_bytes // chunk_size))`. Call it once in `run_sender` after `chunk_size` is validated, and raise `ValueError` for `batch < 1` or `batch_bytes < 1` beside the existing chunk-size check.

- [ ] **Step 4: Add `--batch-bytes` and tighten `--batch` in `pgdelta/cli.py`**

Add `--batch-bytes` (`default=DEFAULT_BATCH_BYTES`) on both `local` and `send`, passed through to `run_sender`. Replace both options' `type=int` with a `positive_int` callable raising `argparse.ArgumentTypeError` below 1: `cli.main` turns a `ValueError` into exit 1, but a bad argument must reach the user as **exit 2**, which only argparse gives. Give `--batch`'s help text the "1 reproduces stop-and-wait" note.

- [ ] **Step 5: Run the whole suite**

Run: `python -m unittest discover -s tests`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add pgdelta/cli.py pgdelta/sender.py tests/test_pgdelta_batch.py
git commit -m "--batch and --batch-bytes, with the batch bounded in bytes (#36, #17)"
```

---

### Task 4: Measure the sweep, set the default, update the docs

The issue's "done when" is a measurement, not a code change: round trips fall by about N, and the benchmark shows the time effect.

**Files:**
- Modify: `pgdelta/README.md`
- Modify: `pgdelta/sender.py` (`DEFAULT_BATCH`, if the sweep says so)

- [ ] **Step 1: Run the sweep on the laptop demo**

Run `examples/local_demo.sh` at `--batch` 1, 16, 128 and 1024, recording for each: `elapsed_seconds`, `counts.round_trips`, `counts.chunks_asked`, `seconds.wait_for_verdict`, and `bytes_sent` by message type.
Expected: `round_trips` ≈ `ceil(chunks_asked / batch)` at every N, and `wait_for_verdict` falling roughly with it from its 62% share.

- [ ] **Step 2: Publish a benchmark run**

Run: `python -m bench run --publish` (or the launcher, `python -m viz serve` → **New run**, with "Commit the run and open a pull request" ticked).
Expected: a run PR whose report shows naive (`--batch 1`) against the chosen N on the same setup.

- [ ] **Step 3: Set `DEFAULT_BATCH` from the sweep**

Change the default in `pgdelta/sender.py` if the sweep's knee is not at 128, and state the measured reason in the commit message. Note for the write-up: the batch cannot fill a long pipe at any N this sweep covers — 1 Gbit at 120 ms needs 1,831 chunks in flight — which is step #18's argument, not a defect of this step.

- [ ] **Step 4: Update `pgdelta/README.md`**

The "How it works" diagram becomes the batched exchange (`CHUNK_HASHES` for N chunks, `VERDICTS` as a bitmap). In the "Why it's naive: the waits" table, the first row ("One network round trip per 8 KiB chunk") becomes the remaining per-batch wait and names `--batch`. Add `chunks_asked` and `batch` to the statistics section.

- [ ] **Step 5: Run the whole suite and commit**

Run: `python -m unittest discover -s tests`
Expected: PASS

```bash
git add pgdelta/README.md pgdelta/sender.py
git commit -m "Batched digests measured: N=<chosen> is the knee on the laptop demo (#36, #17)"
```

- [ ] **Step 6: Open the pull request**

Against `main`, from `pipeline/batched-hashes`, filling the template's four sections: the problem and resulting behaviour; validation and evidence (the sweep table, the published run PR, the suite); limitations and risks (the pipe argument above, `VERSION` 2 requiring both ends deployed together, #37 touching the same files); related issue (#36, step was #17).
