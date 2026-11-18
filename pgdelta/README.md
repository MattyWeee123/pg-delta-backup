# pgdelta, the naive version

Refreshes an old copy B of a PostgreSQL data directory from an up-to-date one, S, sending only the chunks that differ. The result O is built beside B, verified file by file against S, and published with one rename. B itself is never modified.

## Run it

```bash
python -m pgdelta local   --source S --basis B --output O [--chunk-size 8192] [--batch 128] [--batch-bytes 33554432] [--stats-json stats.json]

# or on two machines:
python -m pgdelta receive --basis B --output O --listen 0.0.0.0:7000     # on the destination
python -m pgdelta send    --source S --connect DEST:7000 [--batch 128] [--batch-bytes 33554432]   # on the source
```

S and B are data directories: a cleanly stopped cluster, or base backups. Exit status 0 means O was published and every file verified.

## How it works

```
Sender (holds S)                                  Receiver (holds B)
HELLO: file list, chunk size, session key  ──▶
                                           ◀──    ACCEPT: the sizes of the files B has
for each file, accumulating up to --batch 8 KiB chunks at a time (never past the file's end):
  hash each chunk in the batch
  CHUNK_HASHES (file, first chunk, N digests) ──▶  read B's chunks there, hash them, compare each
  wait …                                   ◀──    VERDICTS (one bitmap bit per chunk in the batch)
  for each chunk that differs: CHUNK_DATA (the bytes) ──▶  write it into O (a same chunk is copied from B)
FILE_DONE (size, whole-file digest)        ──▶    sync the file, re-read it, compare with the digest
COMPLETE                                   ──▶    sync everything, rename O into place
                                           ◀──    RESULT: ok, with the receiver's statistics
```

- **A batch** holds up to `--batch` chunks, never spanning two files, further capped so the accumulated
  chunk data stays under `--batch-bytes` and so one `CHUNK_HASHES` frame still fits the wire limit.
- **Chunks B lacks** (a new file, or past the end of B's file) are sent without asking, after the pending
  batch is flushed so chunk indices on the wire stay ascending.
- **Files only in B** don't appear in O.
- **Digests** are 16-byte keyed BLAKE2b. The key is random for each session, so nobody can prepare a chunk whose digest matches another chunk's.
- **Safety:** the receiver accepts only clean relative paths, never writes through a symbolic link, never overwrites an existing O, and removes its staging directory on any failure.

## Why it's naive: the waits

| Where the time goes | Roadmap step that removes it |
|---|---|
| One network round trip per batch of up to `--batch` chunks (`CHUNK_HASHES`/`VERDICTS`); the sender idles while it waits for that batch's verdicts | Keep more than one batch in flight, instead of waiting for each one before sending the next |
| The receiver reads and hashes B's chunk only when asked, while the network idles | Both sides hash at the same time |
| Hashing, reading and sending take turns on one thread | Overlap them (a pipeline) |
| One CPU core hashes | Hash on worker processes |
| One TCP connection | Parallel connections, measured |
| Each file is read back from disk to verify it | Verify as it's written |
| A coarse check is missing: every chunk is asked about, even in unchanged files | Coarse-then-fine digests (a hash tree) |

`--batch` defaults to 128 chunks per round trip; that default is provisional, not a measured or tuned
value — no sweep across batch sizes has been run yet.

## Statistics

`--stats-json` writes the sender's report, with the receiver's inside it:
- **counts:** files, `chunks_same`, `chunks_changed`, `chunks_new`, `round_trips`, `chunks_asked`, `bytes_read`.
  `round_trips` counts `CHUNK_HASHES`/`VERDICTS` exchanges (one per batch); `chunks_asked` counts the
  chunks those exchanges covered — `round_trips` ≤ `chunks_asked`, equal only when every batch held
  exactly one chunk (as with `--batch 1`);
- **seconds:** time spent in `read`, `hash`, `send`, `wait_for_verdict`, and on the receiver `read_basis`, `write`, `fsync`, `verify`;
- **bytes:** sent and received per message type, with `bytes_source_to_dest` and `bytes_dest_to_source` totals (the benchmark harness reads these).

The report also carries `chunk_size` and `batch`, the settings it ran with.

`wait_for_verdict` is the cost of stopping and waiting: the round trip plus the receiver's read and hash.
