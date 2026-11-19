# pgdelta, the naive version

Refreshes an old copy B of a PostgreSQL data directory from an up-to-date one, S, sending only the chunks that differ. The result O is built beside B, verified file by file against S, and published with one rename. B itself is never modified.

## Run it

```bash
python -m pgdelta local   --source S --basis B --output O [--chunk-size 8192] [--digest-window 67108864] [--stats-json stats.json]

# or on two machines:
python -m pgdelta receive --basis B --output O --listen 0.0.0.0:7000     # on the destination
python -m pgdelta send    --source S --connect DEST:7000 [--digest-window 67108864]   # on the source
```

S and B are data directories: a cleanly stopped cluster, or base backups. Exit status 0 means O was published and every file verified.

## How it works

```
Sender (holds S)                                  Receiver (holds B)
HELLO: file list, chunk size, session key  ──▶
                                           ◀──    ACCEPT: the sizes of the files B has
                                           ◀┄┄    BASIS_DIGESTS (file, first chunk, N digests) —
                                                  streamed unasked, throughout, by a generator thread
for each file, reading and hashing S while B's digests keep arriving (never past the file's end):
  compare each chunk against the next digest waiting in the table (wait only if it hasn't arrived)
  chunks B already holds accumulate into a run:
    CHUNK_COPY (file, first chunk, run length) ──▶  copy that run out of B
  everything else, as soon as it differs or B lacks it:
    CHUNK_DATA (file, chunk, the bytes)        ──▶  write it into O
FILE_DONE (size, whole-file digest)        ──▶    sync the file, re-read it, compare with the digest
COMPLETE                                   ──▶    sync everything, rename O into place
                                           ◀──    RESULT: ok, with the receiver's statistics
```

- **Nothing is asked for.** The receiver's generator thread walks B in the same order as the sender walks
  S and streams a digest for every chunk the sender will compare, before being asked for any of them.
  The sender's reader thread drains those digests into a table on its own thread, so the main thread
  never stops writing in order to listen.
- **A run** is consecutive chunks B already holds, named by one `CHUNK_COPY`; it never spans two files.
  An untouched file costs exactly one run.
- **Chunks B lacks** (a new file, or past the end of B's file) are sent without asking, after the pending
  run is flushed so chunk indices on the wire stay ascending.
- **`--digest-window`** bounds how many bytes of B's undelivered digests the sender's reader thread will
  hold before it stops draining the receiver, so a slow comparison applies backpressure instead of
  growing memory without bound. It defaults to 64 MiB and is floored at one `BASIS_DIGESTS` frame so the
  pipeline can never collapse into synchronous, frame-at-a-time handoff. That default is provisional —
  unmeasured, as `--batch`'s default was before it.
- **Files only in B** don't appear in O.
- **Digests** are 16-byte keyed BLAKE2b. The key is random for each session, so nobody can prepare a chunk whose digest matches another chunk's.
- **Safety:** the receiver accepts only clean relative paths, never writes through a symbolic link, never overwrites an existing O, and removes its staging directory on any failure.

## Why it's naive: the waits

| Where the time goes | Roadmap step that removes it |
|---|---|
| The sender still stops and waits whenever the next chunk's digest hasn't arrived yet (`digest_waits`), because reading, hashing and sending all happen on one thread | Overlap them (a pipeline) |
| One CPU core hashes | Hash on worker processes |
| One TCP connection | Parallel connections, measured |
| Each file is read back from disk to verify it | Verify as it's written |
| A coarse check is missing: every chunk is compared against a digest, even in unchanged files | Coarse-then-fine digests (a hash tree) |

## Statistics

`--stats-json` writes the sender's report, with the receiver's inside it:
- **counts:** `files`, `chunks_same`, `chunks_changed`, `chunks_new`, `round_trips`, `chunks_asked`,
  `digest_waits`, `copy_runs`, `bytes_read`. `round_trips` counts every time the sender blocked on the
  peer — ACCEPT, each digest wait, and RESULT — so it no longer scales with chunks at all. `chunks_asked`
  counts chunks compared against a digest of B; the name is historical, nothing is "asked" any more.
  `digest_waits` is how many of those round trips were spent waiting for a digest that had not arrived
  yet — the interesting number now, since it is the residual stall overlapping read/hash/send (the next
  roadmap step) attacks. `copy_runs` is how many run-length `CHUNK_COPY` instructions were sent; an
  untouched file costs exactly one.
- **seconds:** time spent in `read`, `hash`, `send`, `wait_for_digests`, and on the receiver `read_basis`, `write`, `fsync`, `verify`;
- **bytes:** sent and received per message type, with `bytes_source_to_dest` and `bytes_dest_to_source` totals (the benchmark harness reads these).

The report also carries `chunk_size`, the setting it ran with.

`wait_for_digests` is the cost of stopping and waiting: the round trip plus however long it takes the
receiver's generator thread to catch up and send that digest.
