# Delta Synchronization, Remote Differential Transfer, and Content-Defined Deduplication: Algorithms and State of the Art (for page-aligned DB files)

Scope note: this file covers generic algorithms (rsync family, CDC/dedup, delta-sync research, set reconciliation, hash functions, database-aware dedup, fixed-size vs CDC). PostgreSQL-internal mechanisms (WAL summaries, page header fields, hint bits, `pg_basebackup --incremental`) are covered by the sibling notes `postgres_core_mechanisms.md` and were not re-verified here. Numbers marked "(inference)" are my own calculations from cited parameters, not measured results.

## 1. The rsync family: algorithm, block-size rule, signature cost, modes, large-file behaviour, librsync/rdiff, parallel variants

### Takeaway
rsync (Tridgell & Mackerras 1996/1998 tech report) has the receiver send one weak 32-bit rolling checksum plus a truncated strong hash per fixed-size block; the sender then rolls a byte-by-byte search over its file to find those blocks at any offset. The default block size is about sqrt(file length), with a floor of 700 B and a cap of 128 KiB (protocol 30 and later), so a 1 GiB file uses about 32 KiB blocks. That makes one dirty 8 KiB page cost about 32 KiB of literal data. Both sides still read the entire file, and by default the receiver rewrites the whole file. For data that never shifts, the byte-offset search is pure overhead.

### Cited Findings
**Algorithm (primary sources)**
- The tech report is by Andrew Tridgell and Paul Mackerras (ANU), dated 1998-11-09. It covers updating a file over a "low-bandwidth, high-latency" link by finding the parts of the source that already exist in the destination and sending only the rest. The report says it works best when files are similar but stays correct and "reasonably efficient" when they differ a lot. — [rsync tech report](https://rsync.samba.org/tech_report/)
- Weak checksum: a(k,l) = (Σ X_i) mod M, b(k,l) = (Σ (l−i+1)·X_i) mod M, s = a + 2^16·b, with M = 2^16. The design was "inspired by Mark Adler's adler-32". O(1) recurrences roll it forward one byte at a time. The weak checksum is a cheap first-level filter, and the more expensive strong checksum runs only on weak matches. — [tech report §Rolling checksum](https://rsync.samba.org/tech_report/node3.html)
- Search has three levels: (1) a 16-bit hash of the 32-bit rolling sum indexes a 2^16-entry table; (2) a scan of the sorted list compares the full 32-bit weak sum; (3) the strong checksum is compared. On a match, the sender emits the literal bytes since the previous match plus the block index, then jumps past the block. On a miss it advances one byte. The report notes that runs of consecutive matching blocks could be run-length encoded. — [tech report §Checksum searching](https://rsync.samba.org/tech_report/node4.html)
- Measured results (1996-era code, 24 MB Linux kernel tarballs 1.99.10 vs 2.0.0). Columns are block size / literal data / bytes written by sender / bytes read by sender (signatures):
  - 300 B: 5,312,200 / 5,629,158 / 1,632,284
  - 500 B: 1,091,900 / 1,283,906 / 979,384
  - 700 B: 1,307,800 / 1,444,346 / 699,564
  - 900 B: 1,469,500 / 1,575,438 / 544,124
  - 1100 B: 1,654,500 / 1,740,838 / 445,204
  - False alarms (weak match, strong mismatch) fell from 948 at 300 B to 22 at 700 B.
  - On the 1.7 MB Samba source tree, 300 B blocks gave the least traffic: 153,999 written plus 83,948 read.
  - Source: [tech report §Results](https://rsync.samba.org/tech_report/node6.html)

**Block size and signature size in modern rsync**
- Constants in `rsync.h`: `BLOCK_SIZE 700`, `MAX_BLOCK_SIZE (1<<17)` (128 KiB), `OLD_MAX_BLOCK_SIZE (1<<29)`, `MAX_MAP_SIZE (256*1024)`, `SUM_LENGTH 16`, `SHORT_SUM_LENGTH 2`, `BLOCKSUM_BIAS 10`. Each block's `sum_buf` stores a 32-bit `sum1` (weak). The strong sums live in `sum2_array` at length `s2length`. — [rsync.h (master)](https://raw.githubusercontent.com/RsyncProject/rsync/master/rsync.h)
- `sum_sizes_sqroot()` in generator.c:
  - The comment says "The block size is a rounded square root of file length."
  - A user `--block-size` wins. Files ≤ BLOCK_SIZE² (490,000 B) use 700 B.
  - Larger files get a power-of-two-derived value, capped by a protocol-dependent maximum (MAX_BLOCK_SIZE for protocol ≥30, OLD_MAX_BLOCK_SIZE before that) and rounded to a multiple of 8.
  - The strong-sum length follows Donovan Baarda's formula `blocksum_bits = BLOCKSUM_BIAS + 2*log2(file_len) - log2(block_len)`, clamped between `csum_length` and the maximum digest length.
  - Source: [generator.c (master)](https://raw.githubusercontent.com/RsyncProject/rsync/master/generator.c); older revisions: [mirror](https://mattmccutchen.net/rsync/rsync.git/blob/44cad59f2bc141af50de3583523a22ccbe73bd30:/generator.c)
- Hash table (rsync 3.x `match.c`):
  - Size is `tablesize = (count/8)*10 + 11`, which targets "about 80%" load on big files, with a floor of 2^16.
  - Tables larger than 2^16 index by the full 32-bit weak sum (`BIG_SUM2HASH`).
  - `want_i` prefers the block adjacent to the previous match so the output RLL-codes well.
  - The strong checksum is computed lazily, once per offset (`done_csum2`).
  - Current master caps hash-chain length (`MAX_CHAIN_LEN`). Its comments cite "disk/VM images, which contain large runs of identical blocks", say the unbounded cost "grows with the square of the file size", and mention "multi-hour 'hangs'" on multi-GB images. Data skipped by the cap is sent as literal.
  - Source: [match.c (master)](https://raw.githubusercontent.com/RsyncProject/rsync/master/match.c). I did not determine which release first shipped the chain cap.

**Modes and options (rsync man page, 3.2.x/3.4-era)**
- The default "quick check" skips files whose size and mtime match. `--checksum` instead compares a full-file checksum for every file whose size matches, which reads all data on both sides. — [rsync(1)](https://download.samba.org/pub/rsync/rsync.1)
- `--whole-file`/`-W` disables the delta algorithm. It "is the default when both the source and destination are specified as local paths". It may be faster when network bandwidth exceeds disk bandwidth. `--no-W` forces delta mode. — [rsync(1)](https://download.samba.org/pub/rsync/rsync.1)
- `--block-size` "forces the block size used in rsync's delta-transfer algorithm to a fixed value". The man page says it is "normally selected based on the size of each file being updated". — [rsync(1)](https://download.samba.org/pub/rsync/rsync.1)
- `--inplace` writes into the destination file instead of building a new copy and renaming it, and it implies `--partial`. `--sparse` combined with `--inplace` was rejected before 3.1.3. `--append` updates growing files "without verifying any of the existing content" and is called "dangerous" unless the files are truly append-only. `--append-verify` includes the existing data in the checksum. — [rsync(1)](https://download.samba.org/pub/rsync/rsync.1)
- `--checksum-choice` options: auto, xxh128, xxh3, xxh64, md5, md4, sha1, none. When both sides are 3.2.0 or later (June 2020), they negotiate the strongest mutual algorithm; older peers fall back to MD5 or MD4 by protocol age. `none` forces `--whole-file`. `--compress-choice` options: zstd, lz4, zlibx, zlib, none. — [rsync(1)](https://download.samba.org/pub/rsync/rsync.1)

**librsync / rdiff**
- rdiff has three steps: signature(basis) → sig, delta(sig, new) → delta, patch(basis, delta) → new. It "cannot update files in place", the basis must be random-access, and it does not check that a delta is applied to the right basis. — [rdiff docs](https://librsync.github.io/page_rdiff.html)
- `RS_DEFAULT_BLOCK_LEN = 2048`. The header says the 2K default "assumes a typical file is about 4MB and should be OK for files up to 32G with more than 1GB ram". `RS_DEFAULT_MIN_STRONG_LEN = 12`, "safe for files less than 45TB with a 2KB block_len". — [librsync.h](https://raw.githubusercontent.com/librsync/librsync/master/src/librsync.h)
- There are four signature magics: MD4, BLAKE2, RabinKarp+MD4 and RabinKarp+BLAKE2. MD4 is "strongly deprecated because it creates a security vulnerability" for files containing partly untrusted data. `rs_sig_args()` with strong_len=-1 gives "the smallest signature size that is safe against random hash collisions". — [librsync.h](https://raw.githubusercontent.com/librsync/librsync/master/src/librsync.h)

**Parallel rsync wrappers**
- parsyncfp/parsyncfp2 use `fpart` to split a tree into roughly equal chunks and run one rsync per chunk. The parsyncfp2 man page claims 4–10× speedups on large transfers, says it is ineffective for small transfers, and defaults to 10G chunks and sqrt(#CPUs) processes. — [parsyncfp2(1)](https://manpages.debian.org/unstable/parsyncfp2/parsyncfp2.1)
- msrsync splits a transfer into buckets moved by multiple threads (vendor blog, possible bias). — [Resilio blog](https://resilio.com/blog/parallel-rsync-methods-and-alternatives)

**zsync (rsync over plain HTTP)**
- zsync runs the rsync algorithm on the client. The server hosts only a precomputed `.zsync` control file and serves HTTP Range requests, with no special server software. — [zsync paper](http://zsync.moria.org.uk/paper/)
- Each block gets the same weak checksum as rsync, transmitted as 4 bytes ("optimal in most cases"; 3 bytes is better for some small files), plus MD4 truncated to the length a formula requires (d=20, about one-in-a-million false-match odds). Full-length control data is 20 B per block. — [zsync §Checksum transmission](https://zsync.moria.org.uk/paper/ch02s03.html)
- zsync 0.2.0 can require 2 consecutive matching blocks, which "greatly lowers the probability of a false match" and allows shorter checksums. — [libzypp mediablocklist docs](https://doc.opensuse.org/projects/libzypp/HEAD/mediablocklist_8cc.html); [zsync results](https://zsync.moria.org.uk/paper/ch02s08.html)
- On ~110 MB ISO snapshots, the `.zsync` file was 4.4 MB at 512 B blocks, then 2.2 MB, 1.1 MB and 550 kB as block size doubled. rsync picks larger blocks and sends under 100 kB of metadata. In rsync the checksums travel upstream (receiver→sender). In zsync the client downloads them. — [zsync results](https://zsync.moria.org.uk/paper/ch02s08.html); [comparison](https://zsync.moria.org.uk/paper/ch02s07.html)

**zchunk**
- zchunk splits a file into independently compressed (zstd) chunks so clients fetch only changed chunks via HTTP Range. Files carry strong checksums. An optional shared zstd dictionary must stay the same across revisions; "If the dictionary changes, none of the chunks will match". — [zchunk README](https://github.com/zchunk/zchunk/)

### Inferences
- **rsync on a 1 GiB PostgreSQL segment (inference from the generator.c rule):**
  - sqrt(2^30) = 32 KiB blocks, so 32,768 blocks per segment.
  - Each block carries a 4 B weak sum plus a strong sum of a few bytes up to 16 B. That puts the signature at roughly 0.2–0.65 MiB per segment, about 0.02–0.06% of the file.
  - A single dirty 8 KiB page forces resending its whole 32 KiB block: 4× amplification at low dirty rates.
  - With dirty fraction p scattered randomly across pages, the fraction of 32 KiB blocks to resend is 1−(1−p)^4. That is ≈3.9% at p=1% and ≈34% at p=10%.
  - `--block-size=8192` aligns blocks to pages (PG pages never shift, so matches always occur at aligned offsets). It quadruples the signature to 131,072 entries per segment (~2–2.6 MiB at 16–20 B each).
- **Where rsync spends its work on mostly-unchanged, non-shifting data (inference from the tech report and match.c):**
  - The receiver reads and hashes the entire basis file (weak + strong per block).
  - The sender reads the entire new file and computes a strong hash for every matched block.
  - In every changed block, the sender also rolls byte-by-byte (block_len rolling updates plus hash lookups) before emitting literals. That rolling work is wasted when offsets never shift.
  - Runs of identical blocks, such as all-zero pages in preallocated or empty relation space, cause long hash chains. Current rsync caps those chains explicitly.
- Without `--inplace`, the receiver writes a full new copy of each changed segment: 1 GiB of destination writes even for one dirty page. `--inplace` avoids this but loses the atomic rename (inference from the man page semantics).
- Quick check (size+mtime) is a cheap per-file pre-filter. PostgreSQL segments that were not written keep their mtime and can be skipped entirely; any write touches mtime. The exact PostgreSQL mtime caveats are in the sibling notes.
- **Traffic-optimal block size (inference):** total ≈ (n/b)·s + k·b, with n = file bytes, s = signature bytes per block and k = changed regions. The minimum is at b* = sqrt(n·s/k), which is the origin of rsync's sqrt rule, since rsync assumes k grows slowly.
  - For a 1 GiB segment with s=20: k=10 changed pages gives b* ≈ 46 KB. k=1,000 gives b* ≈ 4.6 KB, which in practice floors at the 8 KiB page.
  - So no single block size is optimal across dirty rates. That motivates hierarchical or Merkle comparison (section 3).
- The rsync tech report table shows the same trade-off empirically. On the kernel tarball, total traffic (written + read) was lowest around 700–900 B blocks (≈2.14 MB and 2.12 MB). On the more heavily edited Samba tree it was lowest at 300 B (≈238 KB). The optimum moves with change density.
- zsync's design (precomputed per-block checksums published once; the client compares against its local copy) maps directly to a "stored manifest" backup design: keep the previous backup's per-page hash list on the backup side so the source never needs the destination to re-read and re-hash its copy (inference).

### Gaps
- I did not retrieve Tridgell's 1999 PhD thesis ([samba.org/~tridge/phd_thesis.pdf](https://www.samba.org/~tridge/phd_thesis.pdf)), so its block-size analysis and multi-round discussion are not summarized here.
- I could not quote the exact `s2length` arithmetic, i.e. whether the 32 weak bits are subtracted from `blocksum_bits`. The per-block strong-sum bytes for a 1 GiB file are therefore given as a range.
- No primary benchmark of modern rsync throughput (GB/s) on multi-GB files with few changes was found. I found no primary statement that rsync's delta engine is single-threaded; this is inferred from the existence of parallel wrappers.
- I don't know which rsync release first added the `MAX_CHAIN_LEN` cap.

## 2. Content-defined chunking (CDC), casync/LBFS/Rabin/FastCDC and successors, and backup-tool dedup designs (restic/borg/kopia); chunk size vs dedup ratio vs metadata

### Takeaway
CDC (LBFS 2001 onward) chooses chunk boundaries from a rolling hash, or from local byte extrema in hashless variants, so insertions shift only nearby boundaries. Its costs are a byte-by-byte scan (0.1–0.2 GB/s for Rabin, about 1 GB/s for Gear, 2.5–3.5 GB/s for FastCDC, 3–10 GB/s for SeqCDC, and up to ~30 GB/s with AVX-512 at 16 KB chunks), variable-size chunks, and an index whose size scales inversely with chunk size. Backup tools use large average chunks (casync 64 KiB, restic about 1 MiB, borg about 2 MiB) to keep metadata small. Smaller chunks find more redundancy, but in LBFS the extra protocol traffic outweighed the gain.

### Cited Findings
**LBFS (Muthitacharoen, Chen, Mazières, SOSP 2001)**
- LBFS examines "every (overlapping) 48-byte region". A region is a breakpoint when the low-order 13 bits of its Rabin fingerprint equal a chosen value, giving an expected chunk size of 2^13 = 8 KB. Minimum chunk size is 2K, maximum 64K. Chunks are identified by SHA-1, and LBFS assumes no collisions because their probability is "far lower than the probability of hardware bit errors". — [LBFS paper](https://pdos.csail.mit.edu/papers/lbfs:sosp01/lbfs.pdf)
- The paper rejects indexing aligned 8 KB blocks because insertions shift boundaries. It also rejects indexing blocks at all offsets, which would need "almost one index entry per byte". — [LBFS paper](https://pdos.csail.mit.edu/papers/lbfs:sosp01/lbfs.pdf)
- Measured on /usr/local: the chunk database was 4.7 MB (1.3% of the data). Median chunk was 5.8K, mean 8,570 B (expected 8,240). 11,379 breakpoints were suppressed by the 2K minimum and 75 inserted by the 64K maximum. — [LBFS paper](https://pdos.csail.mit.edu/papers/lbfs:sosp01/lbfs.pdf)
- LBFS Table 2 (percent of bytes in shared chunks, 48 B window): 2 KB expected chunks 21.30%, 4 KB 19.65%, 8 KB 18.01%. "smaller chunks yield somewhat greater commonality", but "the increased cost of GETHASH and CONDWRITE traffic associated with smaller chunks outweighed the increased bandwidth savings". Window size had little effect. LBFS overall used "over an order of magnitude less bandwidth" than traditional network file systems on common workloads. — [LBFS paper](https://pdos.csail.mit.edu/papers/lbfs:sosp01/lbfs.pdf)

**FastCDC and successors**
- FastCDC (Xia et al., USENIX ATC 2016) combines a Gear-based rolling hash, a simplified cut-point judgment, skipping cut-point checks below the minimum chunk size, and "normalized chunking" to recover the dedup loss from that skipping. It is about 10× faster than Rabin-based CDC and about 3× faster than Gear- and AE-based CDC, with "nearly the same" dedup ratio as Rabin. — [USENIX ATC'16 page](https://www.usenix.org/conference/atc16/technical-sessions/presentation/xia)
- FastCDC TPDS 2020 adds "rolling two bytes each time" and reports 3–12× faster than state-of-the-art CDC with similar or higher dedup ratio. — [TPDS 2020 PDF](https://ranger.uta.edu/~jiang/publication/Journals/2020/2020-IEEE-TPDS(Wen%20Xia).pdf)
- The Rust `fastcdc` crate docs say the 2020 variant should be "30–40% faster" than 2016 per the authors, and measured about 20% on Apple M1, with identical cut points. — [docs.rs fastcdc](https://docs.rs/crate/fastcdc/latest)
- RapidCDC (Ni & Jiang, SoCC 2019) exploits duplicate locality: after one duplicate chunk, the next boundaries are predicted. It reports up to 33× chunking speedup with nearly the same dedup ratio, approaching fixed-size chunking speed on highly redundant data. — [NSF PAR record](https://par.nsf.gov/biblio/10195805)
- SeqCDC (Middleware 2024) and Vectorized SeqCDC (arXiv 2505.21194, 2025; listed as TPDS 2026) are hashless, finding boundaries at monotone byte sequences with content-based skipping. Throughput on Xeon Silver 4314 (Ice Lake):
  - Rabin-style RC and TTTD: 0.12–0.22 GB/s
  - Gear: 0.95–0.98 GB/s
  - FastCDC: 2.5–3.5 GB/s
  - AE and RAM: 1.5–1.9 GB/s
  - SeqCDC: 2.9 GB/s at 4 KB, 5.3–5.4 GB/s at 8 KB, about 10 GB/s at 16 KB
  - AVX-512 SeqCDC: 30.5 GB/s at 16 KB
  - The paper's own abstract says "15×" while its body says "10×"; this internal inconsistency is noted by the source fetch.
  - Source: [arXiv 2505.21194](https://arxiv.org/html/2505.21194v1)
- VectorCDC (Udayashankar, Baba, Al-Kiswany, FAST 2025) vectorizes hashless CDC (AE, RAM) with tree-based extreme-byte search and packed scanning. It reports 8.35–26.2× higher throughput than existing vector-accelerated CDC. — [VectorCDC slides](https://cs.uwaterloo.ca/~alkiswan/papers/VectorCDC_FAST25_Slides.pdf); [paper PDF](https://cs.uwaterloo.ca/~alkiswan/papers/VectorCDC_FAST25.pdf). The PDF text I extracted states a different range that came out garbled (possibly an earlier version), so treat the multiplier as uncertain.
- The same paper measures prior SIMD CDC (SS-CDC, AVX-512) on Ice Lake: SS-CRC 0.51 GB/s (1.59× over CRC) and SS-Gear 1.1 GB/s (1.18× over Gear). It says hash-based rolling CDC vectorizes poorly because of scatter/gather costs, and that chunking and hashing are "the most compute-intensive" dedup phases. — [VectorCDC PDF](https://cs.uwaterloo.ca/~alkiswan/papers/VectorCDC_FAST25.pdf)

**casync**
- casync uses a buzhash rolling hash with a 64K average chunk size by default (`--chunk-size=`) and SHA256 chunk IDs. Each chunk is xz-compressed and stored as a file in a `.castr` store. It can serialize either a block device/image or a file tree. Its author leaves matching chunk size to the squashfs block size as "a research subject for the user". It can seed from local data and use reflinks. — [casync announcement (Poettering, 2017)](https://0pointer.net/blog/casync-a-tool-for-distributing-file-system-images.html)

**restic**
- restic uses Rabin fingerprints over a 64-byte window, with a random irreducible polynomial chosen per repository to hinder watermark attacks. Files under 512 KiB are not split. Blobs are 512 KiB–8 MiB, about 1 MiB on average. Storage IDs are SHA-256. Packs are encrypted with AES-256-CTR plus Poly1305-AES at 32 B overhead per blob, with JSON indexes. — [restic design reference](https://restic.readthedocs.io/en/latest/100_references.html)

**borg**
- borg's default buzhash parameters are (19, 23, 21, 4095): minimum 512 KiB, target about 2 MiB, maximum 8 MiB, 4095-byte window. The buzhash table is XORed with a per-repo secret seed to resist chunk-size fingerprinting. Chunk IDs are a keyed MAC (HMAC) of the plaintext. — [borg internals](https://borgbackup.readthedocs.io/en/stable/internals/data-structures.html)
- borg also has a fixed-size chunker (`--chunker-params fixed,BLOCK_SIZE[,HEADER_SIZE]`), with a block size that is a "multiple of the system page size (usually 4096 bytes) recommended". It is described as low CPU overhead and supports `--sparse`. — [borg internals](https://borgbackup.readthedocs.io/en/stable/internals/data-structures.html)
- borg memory model: about 164 B per chunk plus 240 B per file. The worked example is 1 TiB in about 1 Mi files: 0.31 GiB with default chunking versus 2.8 GiB with `buzhash,10,23,16,4095` (about 64 KiB target). — [borg internals](https://borgbackup.readthedocs.io/en/stable/internals/data-structures.html)

**Chunk-granularity vs dedup (other studies)**
- Meyer & Bolosky (FAST 2011, 857 Microsoft desktops over 4 weeks; Best Paper and 2023 Test of Time) found whole-file dedup achieves about 3/4 of the savings of the most aggressive block-level dedup on live file systems and 87% on backup images. Their slides rank whole-file < fixed-chunk < Rabin on both effectiveness and cost. — [USENIX page](https://usenix.org/conference/fast11/study-practical-deduplication); [slides](https://static.usenix.org/events/fast11/tech/slides/meyer.pdf); [SNIA trace (16 KB fixed blocks)](https://iotta.snia.org/traces/3382)
- Survey: Xia et al., "A Comprehensive Study of the Past, Present, and Future of Data Deduplication", Proc. IEEE 104(9):1681–1710, Sept 2016. — [author PDF](https://ranger.uta.edu/~jiang/publication/Journals/2016/2016-Proceedings%20of%20IEEE-A%20Comprehensive%20Study%20of%20the%20Past,%20Present,%20and%20Future%20of%20Data%20Deduplication.pdf); [OpenAlex](https://api.openalex.org/works/doi:10.1109%2FJPROC.2016.2571298)

### Inferences
- Index or manifest cost per 1 GiB of data, at 16 B/hash (inference):
  - 8 KiB chunks: 131,072 entries, 2 MiB (0.2%)
  - 64 KiB chunks: 256 KiB
  - 1 MiB chunks: 16 KiB
  - The LBFS figure (1.3% including metadata at 8 KB chunks) and the borg figure (about 164 B/chunk in RAM) bracket realistic per-chunk overheads at 20–200 B.
- Backup tools' large chunks (casync 64 KiB, restic about 1 MiB, borg about 2 MiB) are a poor fit for scattered 8 KiB page changes. With 1 MiB average chunks, one dirty 8 KiB page re-uploads about 1 MiB (~128× amplification), and if 1% of pages are randomly dirty, nearly 1−0.99^128 ≈ 72% of 1 MiB chunks change (inference). Generic backup tools amortize metadata at the expense of change granularity.
- CDC scan throughput (1–10 GB/s/core without SIMD) is comparable to or faster than fast NVMe read bandwidth. So CPU is rarely the bottleneck for FastCDC or SeqCDC, but Rabin (≈0.2 GB/s) would be (inference).

### Gaps
- kopia's splitter documentation URL returned 404. Its default splitter and hash (believed to be a DYNAMIC-*-BUZHASH splitter and a BLAKE2 variant) are unverified.
- I could not extract the FastCDC 2016 paper's exact MB/s figures or chunk parameters; the PDF text uses a custom font encoding. Only the abstract multipliers are cited.
- casync min/max chunk sizes are not stated in the source fetched. The common claim of avg/4 and avg×4 is unverified.
- The Meyer & Bolosky per-chunk-size numbers (64K to 8K) were not extractable.

## 3. Research on delta-sync efficiency (WebR2sync+, DeltaCFS, QuickSync, PandaSync, Dsync, dsync, SkySync) and set reconciliation / multi-round / theoretical bounds

### Takeaway
Academic delta-sync work since about 2015 accepts rsync's traffic efficiency and attacks its CPU cost, search cost and round trips. The tools are lighter weak hashes piggybacked on CDC (Dsync), moving search to the more capable side (WebR2sync+), switching between full and delta sync adaptively (PandaSync), and, most effectively for huge binaries, not scanning at all: tracking dirty blocks at write time (dsync, LISA'13) or reusing checksums the storage layer already keeps (SkySync, FAST'26). Theory says the optimum is about t·log n bits for t edits in an n-length file. That is reachable with about log t interactive rounds, or in one round with set-reconciliation sketches (IBF/IBLT) whose size scales with the difference, not the file.

### Cited Findings
**Systems papers**
- WebRsync / WebR2sync+ (Xiao et al., FAST 2018): browser JavaScript was too slow for rsync's client-side search. WebR2sync+ reverses the roles, moving search and comparison to the server with locality-aware chunk matching and lightweight checksums. It is about 10× faster than WebRsync, and one VM can serve 6,800–8,500 concurrent web clients. — [USENIX FAST'18](https://www.usenix.org/conference/fast18/presentation/xiao)
- DeltaCFS (Zhang, Li et al., ICDCS 2017) identifies the "abuse of delta sync": under typical workloads, trading computation for network savings can be highly unfavorable. — [ICDCS'17 listing](https://pkuscholar.onrender.com/conf/icdcs/ZhangLYLLGD17/)
- QuickSync measured mobile cloud sync and found a minor Dropbox document edit could produce sync traffic about 10× the modification. — [Stony Brook record](https://researchconnect.stonybrook.edu/en/publications/quicksync-improving-synchronization-efficiency-for-mobile-cloud-s-2/). The venue and year (believed MobiCom 2015) were not verified.
- PandaSync (Wu, Jiang, Mao et al., ICDCS 2019) chooses full or delta sync by file size and network conditions, and its Full2Sync cuts round trips for small files. It reports average sync-time reductions of 85.1% versus delta sync and 74.6% versus full sync. — [PandaSync PDF](https://ranger.uta.edu/~jiang/publication/Conferences/2019/2019-ICDCS-PandaSync.pdf)
- Dsync (He, Xia, Li et al., MSST 2020) is CDC-based delta sync with a weak hash "FastFp" piggybacked on the CDC rolling hash and a redesigned protocol. It is 2–8× faster and supports 30–50% more clients than WebR2sync+ and a dedup-based approach. — [MSST'20 PDF](https://ranger.uta.edu/~jiang/publication/Conferences/2020/MSST20-DSync(WenXia).pdf) (abstract via search; full text unreachable because of a TLS error)
- dsync (Knauth & Fetzer, LISA 2013 Best Paper; a different system with the same name) tracks block modifications online in the Linux kernel (3.2.35 extension) instead of computing checksums after the fact. It cuts sync time "by up to two orders of magnitude" versus checksum-based methods for hundreds-of-GB data, with gains in disk I/O, cache pollution and CPU. — [LISA'13](https://usenix.org/conference/lisa13/technical-sessions/papers/knauth)
- SkySync (Zhang et al., FAST 2026) uses "collaborative delta generation": it reuses checksums and digests that the storage layer already maintains for integrity, and reuses prior checksum work in the search. Its fixed-size variant extends rsync's protocol and its CDC variant extends dsync's. It reports up to 89.3% less computation and 1.1–2× faster sync at the same network traffic. It notes that even CDC sync schemes "still need to calculate weak checksums of all bytes in the file". — [USENIX FAST'26](https://www.usenix.org/conference/fast26/presentation/zhang-zhihao)

**Set reconciliation and theory**
- Difference Digest (Eppstein, Goodrich, Uyeda, Varghese, SIGCOMM 2011): an invertible Bloom filter plus a strata/sampling estimator of the difference size. It recovers the symmetric difference of two sets in one round, with communication proportional to the difference size (times log keyspace), not set size. It beats Approximate Reconciliation Trees and Characteristic Polynomial Interpolation. — [Google Research page](https://research.google/pubs/whats-the-difference-efficient-set-reconciliation-without-prior-context/); [Eppstein lecture notes](https://www.ics.uci.edu/~eppstein/261/lecture4c.pdf)
- Venkataramanan, Swamy, Ramchandran ("Low-Complexity Interactive Algorithms for Synchronization from Deletions, Insertions, and Substitutions", arXiv 2013 / UCB TR 2015):
  - Lower bound of about t·log n bits for t edits ("≈log n bits to indicate each position").
  - Their multi-round algorithm uses within about (4c+2)·t·log n bits in about log t rounds, with O(n) computation.
  - Their one-round variant uses Θ(n^((1+2b)/3)·log n) bits for t = n^b edits.
  - Prior bounds they cite: Orlitsky–Viswanathan O(t log n (log n + log 1/ε)); Agarwal et al. O(t log² n); general file-sync protocols "O(t log² n) or higher".
  - In their experiment with 500 random edits, rsync sent about 133% of the file size.
  - Sources: [arXiv 1310.2026](https://arxiv.org/abs/1310.2026); [ar5iv full text](https://ar5iv.labs.arxiv.org/html/1310.2026); [UCB TR](https://www2.eecs.berkeley.edu/Pubs/TechRpts/2015/EECS-2015-227.html)
- 2025 follow-up (Ni, Tauz, Gabrys, Dolecek) improves the deletion-only interactive protocol using multi-deletion codes. — [arXiv 2512.06606](https://arxiv.org/html/2512.06606v1)

### Inferences
- For PostgreSQL segments, edits are page substitutions only (no insertions or deletions shift data). The problem therefore reduces from string synchronization to comparing a fixed-length vector of page hashes, or reconciling a set of (page_no, hash) pairs. Theory gives about d·log(N) bits for d dirty pages out of N, plus hash bits to verify content (inference).
- **Three practical protocol shapes (inference):**
  - **(a) One-shot flat manifest:** send all N page hashes. One round trip; 2 MiB per 1 GiB segment at 16 B/hash.
  - **(b) Merkle / hierarchical:** compare segment root, then 1 MiB-region hashes (1,024 × 16 B = 16 KiB per segment), then page hashes only inside differing regions. Bytes are about O(d·fanout·log N) and round trips about log_fanout(N), with 2–3 rounds usually enough. Merkle-tree anti-entropy is the approach of Amazon's Dynamo (SOSP 2007, [paper](https://www.allthingsdistributed.com/files/amazon-dynamo-sosp2007.pdf)); I did not re-fetch that paper in this session.
  - **(c) IBLT difference digest:** one round, size proportional to the estimated d. Most useful when d is small relative to N and the round-trip time is high.
- The strongest lever in the literature is avoiding the scan (dsync's tracking, SkySync's reuse of stored checksums). For PostgreSQL this corresponds to WAL-derived changed-block lists, covered in the sibling notes. A stored per-page hash manifest from the previous backup is the "reuse prior checksum" analogue (inference).
- DeltaCFS and PandaSync both warn that delta sync can cost more than it saves. When a segment's dirty fraction is high (say above 30–50% of pages), sending the whole segment, compressed, may be cheaper in CPU and round trips than per-page diffing (inference; the threshold should be measured).

### Gaps
- I found no USENIX ATC 2020 "Dsync"; the closest matches are MSST 2020 Dsync and LISA 2013 dsync. The full Dsync text and its CPU breakdown of rsync were not retrievable.
- I did not obtain exact IBLT size constants (cells per difference element, typically quoted as about 1.5–2× d for high decode probability).
- No paper found specifically evaluates Merkle-tree versus flat-manifest versus IBLT for page-aligned database files.

## 4. Hash function choices and throughput (SHA-256 with SHA-NI, BLAKE3, xxHash/XXH3, CRC32C) and collision safety

### Takeaway
Per core:
- Non-cryptographic XXH3/XXH128: about 30–60 GB/s.
- Hardware CRC32C: roughly 20+ GB/s with 3-way interleaving.
- BLAKE3: several GB/s, best on inputs of 16 KiB or more.
- SHA-256 with SHA-NI: about 1.5–2 GB/s.
- Software SHA-256 and MD5/SHA-1: under about 0.8 GB/s.

For positional page comparison (page i old vs page i new), a 64- or 128-bit hash is statistically ample unless page contents can be attacker-chosen. Content-addressed dedup across billions of pages needs 128 bits or more, or a cryptographic hash.

### Cited Findings
- xxHash reference benchmark (i7-9700K, Ubuntu 20.04, clang 10 -O3), large-data bandwidth:
  - XXH3 (AVX2) 59.4 GB/s; XXH128 (AVX2) 57.9 GB/s
  - XXH3 (SSE2) 31.5 GB/s; XXH128 (SSE2) 29.6 GB/s
  - XXH64 19.4 GB/s; XXH32 9.7 GB/s; City64 22.0 GB/s
  - SHA1 0.8 GB/s; MD5 0.6 GB/s; memcpy from RAM 28.0 GB/s
  - All variants pass SMHasher.
  - Source: [xxHash README](https://github.com/Cyan4973/xxHash)
- BLAKE3 paper: on an AWS c5.metal (Xeon Platinum 8275CL, Cascade Lake-SP, AVX-512), "peak single-threaded throughput is 4× that of BLAKE2b, 8× that of SHA-512, and 12× that of SHA-256". SIMD parallelism across 1 KiB chunks becomes "visible at 2 KiB and above", and "Beyond 16 KiB, throughput is relatively stable". Multithreaded throughput scales almost linearly up to 16 threads, then levels off on memory bandwidth. — [BLAKE3 paper](https://github.com/BLAKE3-team/BLAKE3-specs/blob/master/blake3.pdf); [BLAKE3 repo](https://github.com/BLAKE3-team/BLAKE3)
- SHA-256 with SHA-NI: a BoringSSL commit measured about 1.57 GB/s at 8–16 KB buffers on an AMD EPYC VM, up from about 437 MB/s without SHA-NI. — [BoringSSL commit](https://boringssl.googlesource.com/boringssl/+/17c8c811049a82d6c34897c718503444529c8d8b)
- A Linux kernel patch (Biggers, 2025) interleaving two SHA-256 messages gained +84% on Zen 1, +98% on Zen 4 and +64% on Zen 5 for 4096-byte messages, because AMD executes SHA-NI concurrently. — [LKML patch](https://lkml.iu.edu/hypermail/linux/kernel/2502.1/06361.html)
- FreeBSD measured about 8,000 Mb/s for SHA-NI versus about 1,800 Mb/s in software on a Ryzen 1950X at 4 KB buffers. — [FreeBSD review D12452](https://reviews.freebsd.org/D12452?id=33386)
- CRC32C (SSE4.2): the instruction has 3-cycle latency and 1/cycle throughput, so fast implementations interleave 3 streams and combine them with PCLMULQDQ. This pays off above about 216 B. — [Go issue 16107](https://golang.org/issue/16107); [memcached crc32c.c](https://codegraph.jelmer.uk/memcached/1.6.45-1/crc32c.c)
- One Rust crate claims about 23 GB/s hardware versus 2.5 GB/s software on a 2.7 GHz i5 (low-authority). — [crc32c-hw crate](https://crates-io.ganymede-static.progval.net/crates/crc32c-hw/0.1.2)
- RocksDB's port of the 3-way CRC32C cut CRC CPU share from about 11.5% to about 4.5% in ReadRandom. — [RocksDB commit](https://git.nextgraph.org/NextGraph/rocksdb/commit/f54d7f5feaff09b0e2bf749ecd77043bcd4141b2)
- Collision-safety positions in deployed systems:
  - LBFS assumes SHA-1 collisions are less likely than hardware errors. — [LBFS](https://pdos.csail.mit.edu/papers/lbfs:sosp01/lbfs.pdf)
  - librsync deprecates MD4 signatures as a "security vulnerability" for partly untrusted data and offers strong_len=-1 as "the smallest signature size that is safe against random hash collisions". — [librsync.h](https://raw.githubusercontent.com/librsync/librsync/master/src/librsync.h)
  - rsync negotiates xxh128/xxh3/xxh64/md5/md4 for transfer checksums. — [rsync(1)](https://download.samba.org/pub/rsync/rsync.1)
  - restic and casync use SHA-256; borg uses a keyed MAC with a secret-seeded chunker. — [restic](https://restic.readthedocs.io/en/latest/100_references.html); [casync](https://0pointer.net/blog/casync-a-tool-for-distributing-file-system-images.html); [borg](https://borgbackup.readthedocs.io/en/stable/internals/data-structures.html)
  - zsync sizes strong-checksum truncation for about one-in-a-million false-match odds per file. — [zsync](https://zsync.moria.org.uk/paper/ch02s03.html)

### Inferences
- **Hashing one 1 GiB segment on one core (inference from the cited rates):** XXH3 ≈ 20–35 ms; CRC32C (hardware) ≈ 50 ms; SHA-256 with SHA-NI ≈ 0.55–0.7 s; software SHA-256 ≈ 2–3 s.
- A fast NVMe read at 2–7 GB/s means XXH3, CRC32C and BLAKE3 keep up on one core. SHA-256 with SHA-NI needs about 2–4 cores per NVMe drive to avoid becoming the bottleneck. Hashing two or more pages concurrently helps on AMD, per the kernel patch.
- At 8 KiB inputs, BLAKE3 gets only about 8 chunks of SIMD parallelism (one per 1 KiB chunk), below its 16 KiB-plus plateau. Hashing per page is therefore less efficient than hashing larger regions. Hashing a 64 KiB–1 MiB region per Merkle node, and per-page only where needed, plays to BLAKE3's strengths.
- **Collision math (inference, birthday bound P ≈ N²/2^(b+1)):**
  - Positional comparison does N independent tests per backup, so P(any false "unchanged") ≈ N·2^-b. For 10 TiB at 8 KiB pages, N ≈ 1.34×10^9: about 7×10^-11 with 64-bit hashes and about 4×10^-30 with 128-bit.
  - Content-addressed dedup over the same N pages compares every pair. P ≈ 5% with 64-bit hashes (unacceptable) and about 3×10^-21 with 128-bit.
  - Non-cryptographic hashes (XXH3, CRC32C) give no guarantee against crafted collisions. Since table contents are often user-controlled, a malicious user could plant pages engineered to collide and corrupt a backup, if the hash alone decides "unchanged". Mitigations: a keyed or cryptographic hash (BLAKE3 keyed mode, SHA-256) for skip decisions, or a secret per-backup seed (borg/restic's approach).
- PostgreSQL's own page checksum (16-bit, when enabled) is too short to serve as a change detector. Covered in the sibling notes; flagged as inference.

### Gaps
- No authoritative single-core absolute GB/s for BLAKE3 or SHA-256 (SHA-NI) on current Zen 4/5 or Sapphire Rapids hardware was found. The BLAKE3 paper's figure is in cycles per byte and was not extractable as numbers. Teams should run `openssl speed -evp sha256`, `b3sum --num-threads 1`, and `xxhsum -b` on target hardware.
- No primary Intel or AMD source for CRC32C GB/s was retrieved.

## 5. Database-aware deduplication and delta research, and how row-level updates map to page-level changes (write amplification)

### Takeaway
Research on dedup for databases (dbDedup, SIGMOD 2017) found that record-level similarity plus byte-level delta encoding beats chunk-hash dedup on DB data: up to 37× reduction in storage and replication traffic, 61× with block compression. For page-based engines, the dominant effect is amplification. A uniformly random update workload dirties about one distinct heap page per update until most pages are dirty, so page-level deltas can be orders of magnitude larger than the logical row changes. PostgreSQL's full-page-write behaviour, measured in WAL, demonstrates the same effect.

### Cited Findings
- dbDedup (Xu, Pavlo, Sengupta, Ganger; SIGMOD 2017, "Online Deduplication for Databases"): similarity-based dedup for online DBMSs. It samples a few chunk hashes per record (a sketch) to find a similar existing record, then applies byte-level delta encoding, reducing both on-disk storage and replication traffic. Implemented in a distributed NoSQL DBMS on four real datasets, it reached up to 37× reduction (61× with block-level compression) with negligible throughput and latency overhead. — [CMU PDL abstract](https://pdl.cmu.edu/PDL-FTP/Database/xu-sigmod17_abs.shtml); [PDF](https://www.pdl.cmu.edu/PDL-FTP/Database/xu-sigmod17.pdf); [MSR listing](https://www.microsoft.com/en-us/research/?p=346241); [Xu PhD thesis 2016](https://kilthub.cmu.edu/articles/thesis/Online_Deduplication_for_Distributed_Databases/6720935)
- PostgreSQL full-page writes (Vondra, EDB blog, 2016-11-23): the first change to a page after a checkpoint logs the full 8 KB page.
  - Over 1 hour at 5,000 inserts/s, a BIGSERIAL key produced about 2 GB of WAL and a UUID key over 40 GB, because random keys touch random index leaf pages, each triggering an FPI.
  - For uniformly random UPDATEs, integer and UUID keys produced almost the same WAL. Most of it was heap, and HOT updates "pretty much eliminate" index WAL.
  - Rebuilding with 4 KB pages cut WAL from about 140 GB to about 90 GB on a uniform-update workload.
  - Source: [EDB blog](https://www.enterprisedb.com/blog/impact-full-page-writes)
- A vendor blog estimates a single 1 KB insert into a table with five indexes can generate up to about 48 KB of WAL right after a checkpoint (low-authority; vendor estimate). — [TigerData blog](https://www.tigerdata.com/blog/write-amplification-in-postgres-the-3-4x-tax-on-every-insert)
- dsync (LISA'13) and SkySync (FAST'26) both show that tracking or reusing change metadata beats scanning for large block-structured state. — [dsync](https://usenix.org/conference/lisa13/technical-sessions/papers/knauth); [SkySync](https://www.usenix.org/conference/fast26/presentation/zhang-zhihao)
- A 2025 pgsql-hackers RFC ("Umbra") proposes remapping post-checkpoint page writes to new physical pages to avoid FPIs. It is a proposal only. — [pgsql-hackers](https://www.postgresql.org/message-id/b563183f-1c5a-4e1f-99fa-2f7b2b83538d.i%40nayishan.top)

### Inferences
- **Balls-into-bins model of page dirtiness (inference):** U uniformly random single-row updates on a table of P pages leave an expected fraction 1 − (1 − 1/P)^U ≈ 1 − e^(−U/P) of pages dirty.
  - With r rows per page, updating a fraction f of rows gives U = f·r·P, so the dirty-page fraction is ≈ 1 − e^(−f·r).
  - Example: 100 rows per page, 1% of rows updated → 1 − e^(−1) ≈ 63% of heap pages dirty. That is about 63× more page bytes than a row-proportional estimate.
  - 0.1% of rows → about 9.5% of pages. 5% of rows → about 99%.
  - Index pages add more dirty pages unless updates are HOT. A non-HOT update dirties a second heap page plus one leaf page per index.
- **Implication for page-level delta backup (inference):** under random OLTP updates, the dirty fraction per backup interval saturates quickly. A daily page-delta of a hot table can approach a full copy, while append-mostly or time-clustered tables (sequential keys) keep dirty pages concentrated and deltas small. Measure dirty-page fraction per relation per interval, from WAL summaries or hash diffs, before committing to a design. Fall back to whole-segment transfer when the dirty fraction is high (PandaSync-style).
- dbDedup-style sub-page (record or byte) delta encoding of each dirty page against its previous version could shrink transfers further when only a few tuples changed per page. Each 8 KiB page then becomes a small binary delta (e.g., an XOR with the old page followed by compression, or VCDIFF/zstd with the old page as dictionary). This is untested here: no source measured per-page binary delta sizes for PostgreSQL (inference).

### Gaps
- I could not access the dbDedup PDF (connection refused). Its specific arguments against chunk-based dedup for small records and its per-dataset numbers are not quoted.
- I found no empirical study reporting dirty-page fractions per hour or day for real PostgreSQL OLTP workloads (e.g., TPC-C or pgbench at various scales). The balls-into-bins numbers are theoretical. Oracle RMAN block-change-tracking or SQL Server differential-backup sizing studies might exist but were not searched.
- No research found on per-page binary delta encoding ratios for PostgreSQL heap or index pages.

## 6. Fixed-size vs content-defined chunking when data is page-aligned and never shifts

### Takeaway
For data where every unit lives at a fixed offset and edits are in-place substitutions, which describes PostgreSQL segment files within one relfilenode, fixed-size, page-aligned chunking is strictly better than CDC:
- It costs no boundary-scan CPU.
- Its chunks line up exactly with the unit of change, so no single page change touches two chunks.
- Its metadata is predictable.
- Positional comparison needs no hash-table lookup.

CDC's benefit, boundary resynchronization after insertions or deletions, never applies here. CDC helps only for cross-file or cross-relation dedup when identical byte runs appear at shifted offsets, as in logical dumps or rewritten tables. Even there, page headers usually defeat it.

### Cited Findings
- CDC exists to solve offset shifting: LBFS rejected aligned 8 KB block hashing because an insertion would "change the hashes of all the file's blocks". — [LBFS](https://pdos.csail.mit.edu/papers/lbfs:sosp01/lbfs.pdf)
- The CDC literature consistently ranks fixed-size chunking cheapest. Meyer & Bolosky's ranking is whole-file < fixed < Rabin in cost and effectiveness for general file systems. — [Meyer & Bolosky slides](https://static.usenix.org/events/fast11/tech/slides/meyer.pdf)
- RapidCDC's best case is "as fast as a fixed-size chunking method". — [RapidCDC](https://par.nsf.gov/biblio/10195805)
- borg offers a fixed chunker for low CPU and recommends block sizes that are multiples of the system page size. — [borg internals](https://borgbackup.readthedocs.io/en/stable/internals/data-structures.html)
- In the Vectorized SeqCDC paper, fixed-size 8 KB chunking on a "TPCC" dataset reached only 37.39% space savings versus about 86–87% for CDC algorithms. — [arXiv 2505.21194](https://arxiv.org/html/2505.21194v1). The dataset's composition (raw DB page files or logical dumps, and its page size) was not verified. This contradicts the page-aligned reasoning only if it consisted of raw, aligned page files. Treat it as evidence that fixed chunking fails when content shifts, as in dumps or when chunk size or alignment does not match page geometry.
- casync notes chunk size should be matched to the underlying block structure (squashfs block size) and leaves the choice open. — [casync](https://0pointer.net/blog/casync-a-tool-for-distributing-file-system-images.html)
- rsync's rolling search finds blocks at any byte offset. Even with fixed-size blocks, the sender's per-byte rolling in unmatched regions is the cost that later systems (Dsync, SkySync) try to remove. — [rsync tech report](https://rsync.samba.org/tech_report/node4.html); [SkySync](https://www.usenix.org/conference/fast26/presentation/zhang-zhihao)

### Inferences
- **Why CDC hurts on page-aligned data (inference):**
  - With an 8 KiB average CDC, boundaries fall at arbitrary offsets within pages. A dirty page usually overlaps 2 chunks, sometimes 3, so about 2× more bytes are resent than with page-aligned fixed chunks.
  - Normalized or minimum-size constraints make this worse at larger averages.
  - CDC also spends 1–10 GB/s/core scanning bytes, against about 0 for fixed chunking.
  - Variable-length chunks require explicit offset and length metadata, where fixed chunks can be addressed by page number.
  - Long runs of identical bytes (zero-filled new pages) produce no CDC boundaries until the max size.
- **When CDC (or similarity dedup) could help a PostgreSQL backup tool (inference):**
  - (a) Dedup across relfilenodes after VACUUM FULL, CLUSTER or table rewrites. Tuple data moves to new pages at new offsets, but page headers, line pointers and LSNs differ, so whole-page hashes rarely match. Sub-page CDC might recover some tuple-payload overlap, at high metadata cost.
  - (b) Backing up logical dumps or non-page files.
  - (c) Cross-database dedup of identical template files: fixed chunking already handles that because the bytes are identical at the same offsets.
- **Recommended granularity (inference):** fixed 8 KiB page hashes as the leaf unit, with larger fixed-size aggregation nodes (e.g., 1 MiB = 128 pages, or per-segment roots) to cut metadata and round trips in the common nothing-changed case. That gives a Merkle shape with page-aligned leaves. Optionally apply a per-page binary delta or compression for dirty pages. CDC can be left out.

### Gaps
- I found no published experiment that directly compares fixed-size and CDC chunking on raw PostgreSQL (or other page-based DBMS) data-file snapshots across versions. The arguments above are first-principles inferences plus general dedup results.
- The composition of the SeqCDC "TPCC" dataset (raw pages or dump) needs checking in the Middleware'24 SeqCDC paper or the dedup-bench repository ([UWASL/dedup-bench](https://github.com/UWASL/dedup-bench)) before using it as evidence either way.
