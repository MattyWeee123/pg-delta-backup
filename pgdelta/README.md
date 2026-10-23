# pgdelta v0 engine

`pgdelta` rebuilds a new copy O of a fresh physical backup S at a destination that already holds an older copy B. Only pages whose keyed hash differs cross the link. Every output file is re-hashed and checked against the source digest before O is published by a single rename.

Python 3.10+, standard library only, POSIX (Linux, macOS). Design: [docs/SPEC.md](../docs/SPEC.md) sections 2 and 4 to 7.

**Status:** tested on synthetic directory pairs and on small PostgreSQL 17 clusters. The cluster tests are skipped when the binaries are missing; set `PGDELTA_TEST_PG_BIN` to point at them. They cover:
- identity parsing;
- a copy of the same cluster, where every page is reused;
- full mode against a different cluster;
- a stopped cluster refreshed from a stale copy after writes, whose output starts and returns the new rows.

Not yet tested: a reconstructed `pg_basebackup` passing `pg_verifybackup` (gate G2), a restore to a recovery target (G3), or a live workload.

## Running it

```sh
# Both ends on one host; the receiver runs as a child process over pipes.
python -m pgdelta local --source S --basis B --output O \
    --pg-controldata /usr/lib/postgresql/17/bin/pg_controldata --stats-json run.json

# Two hosts over TCP: start the receiver first, then the sender.
python -m pgdelta receive --basis B --output O --listen 0.0.0.0:7000 --pg-controldata .../pg_controldata
python -m pgdelta send --source S --connect dest-host:7000 --pg-controldata .../pg_controldata --stats-json run.json
```

`receive --stdio` speaks the protocol on stdin and stdout. An SSH transport would run that command through `ssh`, the way `local` runs it as a child process.

The TCP transport is neither authenticated nor encrypted. Use it only on a trusted lab network or through an SSH tunnel. The receiver warns when it listens on a non-loopback address and accepts exactly one connection.

While it waits for the sender, `receive --listen` reads B into the page cache, read-only and in the background, and stops when the sender connects; `--prefetch-basis off` turns that off. See [Prefetching B while the listener waits](#prefetching-b-while-the-listener-waits).

Without `--pg-controldata` the cluster identity is unknown, and by default every page is sent (full mode). See [Reuse policy](#reuse-policy-the-identity-gate) for `--reuse-any-basis`.

`--pg-controldata` reads S's identity on `send` and `local`, and B's on `receive`. `--basis-pg-controldata PATH` (`receive` and `local`) reads B's identity with a binary of B's major version, because after `pg_upgrade` B may be PostgreSQL 17 while S is 18. It defaults to the `--pg-controldata` binary:

```sh
python -m pgdelta local --source S18 --basis B17 --output O \
    --pg-controldata /usr/lib/postgresql/18/bin/pg_controldata \
    --basis-pg-controldata /usr/lib/postgresql/17/bin/pg_controldata
```

A pg_controldata of another major version warns that the control file's CRC doesn't match and that its output is untrustworthy, yet still exits 0. pgdelta keeps the values it printed, adds an `untrustworthy` note to that identity, and the gate treats it as unknown. The statistics record both identities: `source_identity` and `basis_identity`, also under `receiver`.

Experiment options on `send` and `local`: `--block` (default: the cluster's block size, else 8192), `--node-blocks` (default 128, so 1 MiB nodes) and `--high-churn-node-fraction F`.

Exit codes (SPEC section 3): 0 success, 2 invalid or unsupported input, 3 transfer or I/O failure, 4 verification failure. A failure reported by the other end carries that end's code. An unexpected internal error is logged with its traceback and exits 3.

`--stats-json` is checked before any work starts. Its directory must exist and accept a probe file, and must not be S, B (in `local`) or inside them; otherwise the run exits 2. If writing the statistics fails at the end anyway, the failure is logged as a warning and the exit code stays the transfer's own. Failure statistics are written for every kind of failure, including interrupts and internal errors.

### Engine switches (EXP-06)

Each switch below can be turned on or off on its own, for [EXP-06](../docs/experiments/EXP-06-engine-throughput.md). The output, the pages and bytes sent, and every statistic except timings and the switches themselves are the same for every combination; see [Hashes](#hashes) and [Building and verifying O](#building-and-verifying-o). `local` passes each switch on to its child receiver, so it applies on both sides.

| Switch | Commands | Values (default first) | What it does |
|---|---|---|---|
| `--jobs N` | `send`, `receive`, `local` | min(4, CPUs); `local`: min(4, CPUs / 2) on each side. Or any N ≥ 1 | **A.** N > 1 hashes on N worker processes: S on the sender; B, B's leaf digests and the verification of O on the receiver. `local` starts N on each side. `1` hashes single-threaded in the pgdelta process itself, as before |
| `--rebuild` | `receive`, `local` | `copy-patch`, `gap-fill` | **B.** `copy-patch` copies each file of B into O in large pieces and writes the received pages over them. `gap-fill` copies each run of unreceived pages on its own, as EXP-05 measured |
| `--verify` | `receive`, `local` | `pipelined`, `sequential` | **C.** `pipelined` re-hashes each finished file of O on the worker processes while the next file is written. `sequential` re-hashes each file right after writing it. With `--jobs 1` there are no workers, so verification is always sequential |
| `--hash-backend` | `send`, `receive`, `local` | `process`, `thread` | Not shown in `--help`. `thread` uses N worker threads instead of processes: EXP-05's engine, kept for comparison. Ignored with `--jobs 1` |

- **EXP-06's configurations:**
  - baseline (EXP-05's one thread): `--jobs 1 --rebuild gap-fill --verify sequential`;
  - A only: `--jobs N --rebuild gap-fill --verify sequential`;
  - B only: `--jobs 1 --rebuild copy-patch` (verification is sequential with one job);
  - A + B + C: `--jobs N`, the defaults otherwise.
- **EXP-05 exactly:** `--jobs N --hash-backend thread --rebuild gap-fill --verify sequential`. Its runner passes these flags.
- **The default `--jobs` is min(4, CPUs)** for `send` and `receive`, and **min(4, CPUs / 2) on each side for `local`**, which runs both ends on one host. CPUs are those the process may run on (its affinity mask on Linux). Where that comes to 1 (one CPU, or up to 3 for `local`), the single-threaded engine runs. The value comes from [EXP-06](../docs/experiments/EXP-06-engine-throughput.md), with both ends on the Mac (5 performance and 10 efficiency cores), 2.1 GB, 1% of rows changed and the inputs in the page cache:
  - `--jobs 4` took 1.29–1.40 s, against 2.94–3.17 s with `--jobs 1` (medians of 3; the ranges don't overlap), for 1.18–1.22× the CPU seconds.
  - `--jobs 8` was the best measured setting: another 13–17% faster, with ranges that don't overlap those of 4. EXP-06's pre-registered rule ("the best measured setting") picks 8. **4 departs from that rule on CPU efficiency alone:** 8 used 1.31–1.39× the CPU seconds of `--jobs 1`, above EXP-06's 1.3× bound.
  - That CPU cost was measured on mixed cores, where work that spills onto efficiency cores costs more CPU seconds. On an EXP-04 VM (4 cores, 2 hyperthreads each), EXP-06's Linux check found another reason for 4: 8 processes hashed only 16% faster than 4, for 1.65× the CPU per pass.
  - `--jobs 1` still means the single-threaded engine in the pgdelta process itself. EXP-04 checks the default on Linux against it.
  - Library callers (`run_sender`, `run_receiver`) still default to one job: more needs the main-module guard below.
- **Worker processes** start with `spawn` on every platform, never `fork`, which isn't safe once threads run. With `spawn` each worker is a child of its pgdelta process, which reaps it when the pool closes. So the workers' CPU time and peak RSS reach `wait4`, `getrusage(RUSAGE_CHILDREN)` and `time` of whoever ran pgdelta, and the statistics' `worker_start_method` says so.
  - Linux used to get `forkserver`. Its workers are children of the fork server, which no one waits for, so those figures missed 85–90% of pgdelta's CPU at `--jobs 4`.
  - `PGDELTA_START_METHOD=forkserver` still selects it, for tests only, with that caveat.
  - The workers ignore SIGINT: Ctrl-C reaches the whole process group, and the pgdelta processes shut their pools down.
  - Each worker also exits on its own as soon as its parent process is gone, even after SIGKILL.
- **Calling pgdelta from Python** with `jobs` above 1 (`run_sender`, `run_receiver`) needs the usual multiprocessing guard in the main module, `if __name__ == '__main__':`. `spawn` and `forkserver` import the main module in every worker. `python -m pgdelta` and `python -m unittest` are already safe.

### Prefetching B while the listener waits

`receive --listen` reads B into the page cache while it waits for the sender to connect. The session key is random per run and arrives in the sender's HELLO, so the receiver can't hash B before then, and the key's security property is unchanged. But it can read B. Without that, its first pass over B, hashing it for the sender, reads B from disk whenever B isn't cached (after a reboot or a cache drop, or when B was written long ago), on the transfer's critical path.

| Option | Values (default first) | What it does |
|---|---|---|
| `--prefetch-basis` | `on`, `off` | `on`: a background thread reads B's files from the moment the receiver listens until the sender connects |
| `--prefetch-cap BYTES` | half of `MemAvailable`; elsewhere a quarter of RAM | At most this many bytes of B are read |

- **What it reads.** B's regular files in B's inventory order. The sender lists S's files in the same order, so that's the order in which the receiver hashes B's.
- **How it reads.** Sequentially, in 8 MiB pieces into one reused buffer. Where the platform has `posix_fadvise` (Linux), the next piece is requested with `POSIX_FADV_WILLNEED` while the current one is copied.
- **It never writes.** Every file is opened with `O_RDONLY`, `O_NOFOLLOW` and `O_NONBLOCK`, and with `O_NOATIME` where the kernel allows it (the files' owner), so not even B's access times change.
- **When it stops.**
  - **The sender connects.** The receiver's own reads of B start at once, in the same order, and a second sequential reader would compete with them for the disk.
  - **It has read every file, or the cap.**
  - **A read fails.** The error is recorded and never raised. The receiver's own reads report real problems with B, as before.
  - **The command ends in any other way:** a failure while waiting, SIGINT or SIGTERM. The thread finishes its current piece and exits; `receive` exits 3 as before, with no O and no staging directory.
- **Why the cap.** The page cache evicts the least recently used pages first, and the files read first are the ones the receiver needs first. A prefetch larger than what can stay cached would evict its own first files before the receiver reads them, so B would be read twice instead of once. Half of what the kernel reports available (`MemAvailable` in `/proc/meminfo`) leaves the other half for what the run caches while the prefetched files must stay: O as it is written, fsynced and re-read for verification, and B again for copy-then-patch. When B is larger, its first cap bytes are prefetched and the rest is read as before. On the EXP-04 VMs (32 GB) the cap is about 15 GB, so a 2.1 GB B is read whole.
- **What it changes:** nothing but timings and its own statistics. The output, the digests, the bytes and pages sent are the same with it on or off, and verification is untouched: every output file is still re-read and checked against the source digest.
- **Where it applies.** Only `receive --listen` waits for its sender. `receive --stdio`, and so `local`, is connected from the start and never prefetches.

The receiver logs one line when the sender connects, for example `prefetched 2120000000 of 2120000000 basis bytes (1019 of 1019 files) in 1.2 s, while waiting 4.0 s for the sender; stopped: finished`, and reports the same in its statistics under `prefetch` (see [Statistics JSON](#statistics-json)).

### Reuse policy (the identity gate)

The identity gate is an efficiency policy, not a correctness requirement. Correctness comes from verification: every output file, including every page copied from B, is re-hashed and checked against the source digest before O is published. A wrong or unrelated basis can't produce a wrong O. At worst it wastes the digest traffic of the comparison.

- **`same-cluster` (default).** pgdelta compares with B only when both identities are known, trustworthy and equal. That covers system identifier, catalog version, pg_control version, block size, blocks per segment and data checksum version. Otherwise it uses full mode: it sends every page and exchanges no digests, since a different cluster or build seldom has equal pages at the same offsets.
- **`any-basis` (`--reuse-any-basis`, on `receive` and `local`).** pgdelta compares with any basis. Use it when B shares pages with S although the identities differ or are unknown. One example is a basis from before `pg_upgrade --copy` or `--link`, which keeps user relation files and, since PostgreSQL 15, their file names.

`--assume-same-cluster` is the older name of `--reuse-any-basis` and now means the same. It used to apply only when an identity was unknown, and a known mismatch still meant full mode.

ACCEPT and the statistics record the policy (`reuse_policy`), the decision (`mode`) and what the comparison found (`gate_reason`).

## What the tool refuses

The sender refuses an S that contains symlinks, special files, anything in `pg_tblspc`, or a `postmaster.pid`. v0 supports plain-format backups only.

The receiver refuses in these cases:
- O already exists.
- O's parent directory is missing.
- O's parent is B or lies inside B.
- B contains `postmaster.pid`.
- B contains symlinks, tablespaces or special files.
- The sender sends any path that is not a normalized relative POSIX path. Absolute paths and empty, `.`, `..`, NUL or backslash components are rejected.

`local` also refuses when S and B are the same directory, when one lies inside the other, and when O's parent is S or lies inside S.

These checks compare filesystem identity (`st_dev`, `st_ino`), not path strings. So 'Basis' and 'basis/O' on a case-insensitive volume, NFC and NFD spellings, symlinks, bind mounts and hard-linked directories can't disguise one directory as another. The receiver walks from O's parent up to `/` and compares every directory with B, with every directory found in B's scan (which catches a mount of one of B's subdirectories) and, in `local`, with S. It repeats the check just before it creates the staging directory, then checks the new staging directory itself and removes it if it landed inside B or S. One case can't be recognized: a directory reached through two mounts that report different `st_dev`, such as one NFS export mounted twice.

## Hashes

Hashing uses keyed BLAKE2b with a 16-byte digest and a 32-byte random key per session. The sender generates the key and sends it in HELLO.

| Level | Input | Personalization |
|---|---|---|
| leaf | one page (the last page may be short) | `pgdelta-leaf` |
| node | concatenated leaf digests of `node_blocks` pages | `pgdelta-node` |
| file | file size as 8-byte big-endian, then the concatenated node digests | `pgdelta-file` |

Each node is one unit of work. Its byte range is read with positional reads (`preadv`), at most 1 MiB at a time into a buffer each thread or process reuses, whatever the node size. Then come its leaf digests and its node digest. Results are joined in node order, so the tree is the same however the work is spread:
- **`--jobs 1`:** every node in the calling thread.
- **`--jobs N`, processes (default):**
  - **Tasks.** A file's nodes go to N worker processes in tasks of consecutive nodes, about 8 MiB each, and at least N tasks when the file has enough nodes.
  - **Reading.** A worker opens the file by path and checks that its device and inode are those of the file the caller opened. It returns each node's digest and leaf digests.
  - **The key.** The session key reaches every worker once, when the pool starts.
  - **Interpreter locks.** Each process has its own, so hashing scales with N.
- **`--hash-backend thread`:** one node per task on N threads sharing the caller's file descriptor. `hashlib` releases the interpreter lock while it hashes a page, but taking it back after every 8 KiB costs more than the hash beyond about 3 threads (EXP-05).

A file of one node, or one task's worth, is hashed in the calling thread. Each caller keeps at most 2N tasks queued (verification N), so the receiver's basis thread, its leaf re-reads and its verification share the workers. Every task a call started has finished when the call returns or fails, even if the pool is being closed from another thread at that moment. If a file's length changes while it is read, it is hashed again in one sequential pass, so the size check that follows reports the change.

The comparison is positional: page *i* of S is checked only against page *i* of the same path in B. A node or page is reusable only if its byte range has the same, non-zero length in both files and the digests match.

Nodes whose digests differ, and the one node that straddles the end of the shorter file, get leaf digests. Every other node of S that lies beyond the end of B is sent whole. With `--high-churn-node-fraction F`, a file in which more than F of the comparable nodes differ skips leaf digests, and those nodes are sent whole.

## Protocol (version 1)

Each frame is a 4-byte big-endian payload length, a 1-byte type, then the payload. Frames over 64 MiB and unknown types are rejected. JSON is compact UTF-8. Binary integers are unsigned big-endian.

| Type | Direction | Payload | Byte category |
|---|---|---|---|
| 1 HELLO | S→D | JSON (see below) | control |
| 2 ACCEPT | D→S | JSON `{mode, reason, reuse_policy, basis_identity, basis_files: [[index, size], ...]}` | control |
| 3 BASIS_NODES | D→S | u32 file index, u64 basis size, node digests | node_digests |
| 4 LEAF_REQUEST | S→D | u32 file index, then a u32 node index per node | leaf_digests |
| 5 LEAF_DIGESTS | D→S | u32 file index, then per node: u32 node index, u32 leaf count, leaf digests | leaf_digests |
| 6 PAGE_DATA | S→D | u32 file index, u64 first page, raw bytes of one or more consecutive pages | page_data |
| 7 FILE_DONE | S→D | u32 file index, u64 size, 16-byte source file digest | control |
| 8 COMPLETE | both | JSON: S→D `{files, directories}`; D→S `{published: true, receiver: {...}}` | control |
| 9 ERROR | both | JSON `{exit_code, message}` | control |

HELLO contains:
- `protocol_version`, `block`, `node_blocks`
- `key`: base64
- `source_identity`: an object or null
- `root_mode`
- `directories`: `[[path, mode], ...]`
- `files`: `[[path, size, mode], ...]`

The receiver rejects missing or unknown HELLO fields. File indexes refer to the `files` list. Modes are the permission bits (0o777); setuid, setgid and sticky bits are not carried.

### Session flow

1. The sender sends HELLO.
2. The receiver validates every path and applies the identity gate against B under its reuse policy. It replies ACCEPT, listing the S files that also exist in B.
3. In reuse mode a receiver thread hashes those basis files in index order and sends BASIS_NODES for each. Full mode sends no digests.
4. The sender hashes each S file in order and compares nodes. It sends all LEAF_REQUESTs for that file at once, in chunks that keep each reply under half a frame.
5. It keeps up to 4 files hashed ahead, so leaf round trips overlap hashing.
6. For each file in turn, it sends PAGE_DATA for the differing and new pages in increasing page order, coalesced into runs of up to 1 MiB, then FILE_DONE.
7. The receiver writes received pages at their offsets and takes every other page from the same offset in B; see [Building and verifying O](#building-and-verifying-o). A whole unchanged file is a straight copy.
8. The receiver fsyncs the file, re-hashes all of it, and compares the result with the FILE_DONE digest, either right away or on the workers while it writes the next file. A mismatch is a fatal verification error.
9. After the sender's COMPLETE, the receiver waits until every file has been verified. Then it applies directory modes and fsyncs every directory. It writes the receipt with status `verified`, renames the staging directory to O, fsyncs O's parent and rewrites the receipt with status `published`. Then it replies COMPLETE with its own statistics.
10. Either side may send ERROR at any point and stop.

Neither side can block the other. The sender drains the small destination-to-source messages on its own reader thread. The receiver's main loop keeps reading while a separate thread sends BASIS_NODES.

The receiver keeps basis leaf digests from its first pass, up to 64 MiB of digests, so a LEAF_REQUEST rarely re-reads B.

### Building and verifying O

**Copy-then-patch (`--rebuild copy-patch`, default).** For a file that B also has, the receiver copies B's file into the staging file in large pieces and writes each received page over it at its offset:
- **How it copies.** On Linux it uses `copy_file_range`, so the kernel copies, or clones on filesystems that share extents. Where that's missing or refused, and on macOS, it uses `pread`/`pwrite` through one 4 MiB buffer.
- **When it copies.** Only as far as needed, in pieces of at least 4 MiB: just before a received page lands beyond what has been copied, and after the last page for the rest of the file.
  - Each byte of B is copied at most once, and only up to the shorter of the two lengths.
  - A file whose pages are all sent from its start copies nothing.
- **Growth, truncation, new and deleted files:**
  - Pages past B's end are always sent and simply written.
  - The output ends at the source's length.
  - A new file is built from received pages only.
  - A file only in B is never opened.
- **Protocol checks.** Before anything is fsynced, every page that wasn't received must exist in B at the same offset and length. Otherwise the run fails with "neither sent nor present at the same length in the basis", exactly as with gap-fill.
- **If B changes during the copy,** verification catches it, like any other wrong byte. A change of B's size is reported as such.

**Gap-fill (`--rebuild gap-fill`)** is the EXP-05 engine. After the last page of a file arrives, each run of pages that wasn't received is copied from B with its own 1 MiB reads and writes. With scattered changes that's tens of thousands of small copies.

**Pipelined verification (`--verify pipelined`, default with `--jobs` above 1):**
- **Overlap.** Each finished file is fsynced and closed. Then a background thread re-hashes it on the worker processes while the main loop writes the next file.
- **Failure.** The first mismatch is reported to the sender at once and fails the run with exit code 4.
- **Publication.** After COMPLETE, the receiver waits for every verification before it writes the receipt or renames anything. It refuses to publish unless every file was verified.
- **Strength.** The same as sequential: every byte of every output file is re-read and its digest compared with the source's. A file's mode is applied after it passes.

**Sequential verification (`--verify sequential`, and always with `--jobs 1`)** re-hashes each file right after it's written, before the next file is accepted.

### Output safety

O is built in `.<O name>.pgdelta-staging-<random>` next to O, on the same filesystem. Directories and files come from the source inventory only, so files that exist only in B are not carried into O.

Files are created with `O_EXCL | O_NOFOLLOW`. B is only opened read-only, with `O_NOFOLLOW`.

On any error, interruption (SIGINT or SIGTERM) or lost connection before the rename, the receiver deletes the staging tree and its receipt and never creates O. A receiver killed with SIGKILL leaves its staging directory behind, and maybe a `verified` receipt.

### Completion receipt

The receipt is `<O>.pgdelta-receipt.json`, a sibling of O and never inside it, so it can't be mistaken for a backup file (SPEC section 4). It's replaced atomically (temporary file, fsync, rename, fsync of the directory) and holds:
- `schema_version` (1), `tool`, `tool_version`, `output` (absolute path), `mode`
- `status`: `verified` before the rename, `published` after it
- `verified_at` and `published_at` (UTC)
- `files`, `directories`, `bytes`
- `files_digest`: unkeyed BLAKE2b-256 over every file's path, size and keyed file digest, in inventory order. The sender's statistics carry the same value, which matches a run to its receipt. The session key isn't stored, so the value can't be recomputed from O later.

Reading it: O can only appear through the rename, and the rename happens only after the `verified` receipt is durable. So O existing next to a `verified` or `published` receipt means O is complete and verified. A `verified` receipt with no O means the run stopped before publishing. A receipt left from an earlier run whose O is gone is stale and gets replaced. If the receipt path is a directory or a symlink, the run is refused.

### Failures after the rename

The rename is the commit point. After it, a verified O is never deleted.

- **Durability fails.** The fsync of O's parent or the `published` rewrite of the receipt fails. That's an I/O failure: the receiver reports it to the sender and both exit 3, with a message saying O was published but not made durable. O stays, and the receipt may still say `verified`.
- **Interrupted while finishing.** SIGINT or SIGTERM during those last fsyncs is held back until they finish, which takes milliseconds. Then it's honored: the receiver skips the acknowledgement and ends as in the next case.
- **Acknowledgement fails.** The sender is gone, or a signal arrives while the receiver sends its final COMPLETE. The receiver logs `published but acknowledgement failed` and **exits 0**. Its statistics have `acknowledged: false` and `acknowledgement_error`. The sender never got the confirmation, so it exits 3. Its error message points at the receipt, and its statistics carry `files_digest`.

Why 0 and not a new exit code: SPEC section 4 defines success as a verified, complete output plus a durable completion record. At this point both exist, so on the destination O is as good as after a fully acknowledged run. A non-zero code would make a destination-side script throw away or redo a good backup. A new code would be outside SPEC section 3's set, and every caller would have to special-case it. The side that lost information, the sender, still reports the failure.

## Statistics JSON

`--stats-json` holds:
- `schema_version` (1), `status` (`complete` or `failed`), `exit_code`, `error`
- `mode`, `gate_reason` and `reuse_policy` (`same-cluster` or `any-basis`), `block`, `node_blocks`, both identities
- `jobs` and `hash_backend`: the sender's (`serial`, `process` or `thread`)
- `worker_start_method`: how the sender's worker processes were started (`spawn`; null without them). The receiver's statistics have their own
- `switches` `{sender: {jobs, hash_backend}, receiver: {jobs, hash_backend, rebuild, verify}}`: the engine settings in effect on each side. `verify` is `sequential` whenever there's no pool of workers. The receiver's part is null until its final statistics arrive
- `files_digest`: the combined digest also stored in the receipt; null until every file has been sent
- `files` `{total, unchanged, changed, new, size_mismatch}`
- `pages` `{total, sent, reused, sent_bytes}` and `nodes` `{total, matched, descended, sent_whole}`
- `bytes_source_to_dest` and `bytes_dest_to_source`, each `{total, control, node_digests, leaf_digests, page_data, by_message_type}`
- `timings_seconds`, the time in each phase:
  - `hash_source`: hash S;
  - `hash_basis`: hash B, plus re-reads of B for leaf digests;
  - `protocol`: from HELLO to the final COMPLETE;
  - `rebuild`: write O, meaning page writes, copies from B, fsyncs, and the final directory fsyncs and rename;
    - `copy_basis`: of which copying from B;
    - `fsync`: of which fsyncing O's files;
  - `verify`: re-hashing O. With pipelined verification this overlaps writing O;
    - `verify_wait`: the part the receiver's main loop waited for (sequential: all of `verify`);
  - `pool_start`: creating the sender's pool, which starts its workers without waiting for them;
  - `pool_first_ready` and `pool_ready`: from creating the pool until the first, and until the last, of its N workers had started (each reports its pid when its start-up is done). Work queued meanwhile runs on the workers already up, so the start-up cost on the critical path lies between the two;
  - `receiver_pool_first_ready` and `receiver_pool_ready`: the same for the receiver. Any of these four may be null if a short run ends first, and is 0 with `--jobs 1`;
  - `total`.
- `receiver`: the receiver's own view, including its `receipt` path, `files_digest`, `jobs`, `switches` and these timings in seconds. It also holds `pages_copied` and `bytes_copied`, the pages of O taken from B, which are the same for both rebuilds, and `basis_copy_bytes` and `basis_copies`, which depend on the rebuild:
  - **copy-patch:** bytes copied from B, including bytes later overwritten, and one operation per copy-ahead or end-of-file copy;
  - **gap-fill:** one operation per 1 MiB piece of each gap.
- `receiver.prefetch`: what the [prefetch of B](#prefetching-b-while-the-listener-waits) did before the sender connected (null when `run_receiver` is called without it):
  - `switch` (`on` or `off`), `ran`, and `why_not` when it didn't run (`off`, or `stdio`);
  - `files`, `basis_bytes` and `cap_bytes`: what B holds and the cap;
  - `files_read` (to their end) and `bytes`: what it read;
  - `seconds`, the time it read, and `waited_seconds`, from its start until the sender connected;
  - `started_utc` and `ended_utc`: when it started and stopped reading, in seconds since the epoch on the receiver's clock, so a caller timing the receiver from outside can place the reading against its own intervals (null when it didn't run);
  - `stopped`: `finished`, `cap` or `error` if it ended on its own, else why it was stopped (`connected`, `interrupted`, `failed`);
  - `fadvise` (whether `POSIX_FADV_WILLNEED` was used), `noatime` (whether `O_NOATIME` was accepted) and `error`.

The receiver's own statistics (what `run_receiver` returns) also have `acknowledged` and `acknowledgement_error`.

Byte counts include the 5-byte frame headers, but not TCP/IP overhead. The categories sum to the total. In full mode, a file that exists in B counts as `changed` and `unchanged` is 0, because nothing was compared.

## Known limitations

- There's no timeline-history check yet (SPEC section 4). The gate compares system identifier, catalog version, pg_control version, block size, blocks per segment and data checksum version, and decides only whether to compare pages.
- Ownership, extended attributes and timestamps are not carried.
- `pg_control` and `backup_label` get no special ordering, and no `backup_manifest` is emitted.
- Other limits:
  - No resume.
  - No compression.
  - No cross-file matching.
  - No timeouts on a peer that stalls without closing.
- The HELLO inventory must fit in one 64 MiB frame, which is roughly 700,000 files.
- The fsync of each output file stays in the receiver's main loop, before the file goes to verification. On Linux, flushing a large file can cost more than writing it; `timings_seconds.fsync` shows how much.
- Verification re-reads O through the page cache, as it always has, so it checks what the kernel will write, not the medium itself.
- With `--jobs N`, `local` runs N workers on each side, 2N in all, on one host.
