# EXP-68: With the receiver's syncs off its main loop, does the fast path beat a full copy on scattered changes?

Status: **pre-registered October 28, 2026, before any run.** One VM pair (the `ha` set), about 2 hours with the data load, after EXP-67. It follows from EXP-64b's and EXP-66's results: their "what follows" sections name each part of it.

## Question

With 8 reader threads, the fast path refreshed EXP-64's scattered cell (18 GB, 1% of rows, 28% of pages) in 102.8 s (EXP-64b), against 91.0 s for a full copy. The runs' timers, read correctly (EXP-64b, correction 1), say why it isn't faster:

| EXP-64b, `fast8`, uniform | Seconds |
|---|---:|
| Receiver waiting for data | 41.1 |
| Receiver writing and syncing, in its main loop | 59.5 |
| of which syncing each file before the next (`--fsync main`, the default) | 49.4 |
| Sender blocked sending to the receiver | 61.9 |
| Sender waiting on its reader threads | 22.4 |

The receiver received, wrote, then synced each file in turn. While it synced, it didn't read the socket, so the sender blocked. When it read again, the sender's readers had to catch up.

Each side's disk work is about 60 s on its own (**inferred**, EXP-66's rates):
- **dest:** about 249,193 scattered writes, one per run of the plan, at the 4,195 operations a second the disk allows: 59 s. The receiver's writing and syncing took 59.5 s.
- **source:** the same runs read at a depth of 8, which EXP-66 measured at the disk's cap of 4,198 a second: 59 s.

**If the two overlapped,** the refresh would take about 60 s plus about 5 s of fixed cost: about 65 s, below a full copy's 91 s at 28% of pages changed.

pgdelta already has `--fsync worker`: it syncs each finished file on a background thread while the main loop goes on, and every sync completes before the commit (EXP-10, EXP-14). It was measured only at 2.1 GB, where it saved 0.3–2.0 s. **Does it let the two sides overlap here?**

Four more things settle open items:
- **16 reader threads:** EXP-64b's failed prediction 4 asks for a thread sweep. At a depth of 8 the disk is already at its cap, so 16 should add nothing.
- **The disks' counters:** EXP-64b's claim that the receiver's writes are one per run, bound by the disk's operation rate, is inferred. `/proc/diskstats` tests it.
- **The real page set:** EXP-66's model assumed independent pages; its decision on idea 2 (gap-filled writes) is re-applied to the real plan.
- **The hot cell:** read-ahead and the worker must not slow the case the fast path is best at (EXP-64: 1.98 s) before either becomes a default.

## Design

EXP-64's and EXP-64b's set-up, unchanged: `experiments/network/lab_exp64.sh`, whose defaults still reproduce EXP-64's plan (checked).
- **The VMs:** the `ha` set in us-west1, two `n2d-highcpu-8` (8 vCPUs, 8 GB), each with a 150 GiB balanced persistent disk, unthrottled; `blake3` on both.
- **The source:** EXP-48's database (scale 600, about 18 GB, its seed), PostgreSQL 17.11 or later, `summarize_wal = on` before any B0 is taken.
- **The cells:**
  - **hot**, then **uniform**, as in EXP-64 (EXP-48's workloads, 1% of rows each);
  - per cell, B0 is a `pg_basebackup` with its manifest, taken before the change.
- **Each run** starts from a fresh copy of B0, with both VMs' caches dropped. Every pgdelta arm is live, in place, `--jobs 4 --hash blake3 --fast-path summaries`. The arms:
  - **fast:** reads in the sending thread (`--read-ahead 0`), syncs in the main loop (hot only);
  - **fast8, fast16:** 8 or 16 reader threads, syncs in the main loop (uniform only);
  - **fast8w, fast16w:** the same with the receiver's `--fsync worker` (fast8w in both cells, fast16w uniform only);
  - **full:** one `pg_basebackup -X stream -c fast` into a new directory (uniform only).
- **The schedule:** 2 runs per arm, in a shuffled order (seed 680): `PATTERNS="hot uniform" ARMS_hot="fast fast8w" ARMS_uniform="fast8 fast16 fast8w fast16w full" PLAN=1`, `lab_exp64.sh SOURCE 2 680`, results in `exp68`.
- **New in the harness:**
  - an arm `fastNw` adds the receiver's `--fsync worker`, and `ARMS_<pattern>` sets a cell's own arms;
  - `/proc/diskstats` for the disk behind `/data` is recorded on both VMs just before t0 and just after t1;
  - every fast run writes its plan (`--plan-out`, new in pgdelta: each listed file's chosen pages as the runs sent), and its digest goes in the run's record; the first plan per cell is kept.
- **Correctness:** each fast copy is started (recovering from its backup_label), and its `pgbench_accounts` row count and sum of `abalance` must equal the source's.
- **A smoke first** at scale 20 (one run per arm), which decides nothing. It must also have recorded both cells' plans and four counter lines per run.

**The analysis,** committed before the data: `analysis/exp68.py`, figure `exp68-syncs`, data `docs/experiments/data/exp68-cells.csv` and `exp68-model.csv`. Medians of 2. It was dry-run on synthetic results shaped like the harness's output.

## Predictions

On the uniform cell unless named.

1. **Deciding: `fast8w` takes at most 0.85 of `fast8`'s time.** The model above gives about 0.63. A bar at 0.85 (87 s) still needs the overlap to recover more than 15 s of the receiver's 41 s of waiting.
2. **`fast8w` takes at most 1.0 of a full copy's time:** the fast path beats `pg_basebackup` at 28% of pages changed. The model gives about 0.72.
3. **More threads add nothing:** `fast16w` within 10% of `fast8w`.
4. **The receiver's writes are one request per run:** in `fast8`, dest's disk completes 0.7 to 1.5 write requests per run of the plan.
5. **No loss in the hot cell:** `fast8w` takes at most max(`fast` + 0.3 s, 1.15 × `fast`).
6. **Every run is correct and as configured:**
   - every run and every pgdelta receiver exited 0;
   - every fast run used the fast path, its guard passing on B0's `backup_manifest`, and its copy matched the source;
   - the `w` arms synced on the worker and the others in the main loop;
   - each cell's plans were identical;
   - every threaded uniform run read as many runs as the plan lists.
7. **Controls:** `fast8` within 10% of EXP-64b's 102.77 s, and the full copy within 5% of its 90.96 s.

**EXP-66's decision rule, re-applied on the real uniform page set** (its model and `fio` rates unchanged, each file a segment): idea 2 is prototyped if both hold:
- the model without filling is within 30% of `fast8`'s receiver writing and syncing (`rebuild_s`, its syncs included);
- the best fill cuts the modelled write time by at least 25%.

**Reported, not predicted:**
- `fast16` against `fast8`;
- the source's read requests, and both disks' busy time;
- the receiver's sync time on the worker, and how long its main loop waited for it.

**Rule 11:** a deciding ratio within 0.05 of its bar gets a second session.

## What follows

- **If 1 and 2 hold:** at 28% of pages, the fast path beats a full copy with no new algorithm. Research loop 1's crossover estimate (section 3) moves, and the selector (idea 4) is re-sized to the new constants.
  - **If 3 and 5 also hold:** `--read-ahead 8` and the worker become the fast path's defaults. The smoke's in-memory runs are reported beside that change.
- **If 1 holds and 2 fails:** the overlap helped, but a full copy still wins at 28%. The disk counters show which side binds, and idea 2's rule says whether gap-filled writes are next.
- **If 1 fails:** the syncs weren't what held the receiver. The counters and timers say what did: the disks' operation rates, or the verifier's re-reads of written pages competing with the writes.
- **If 4 fails:** the receiver's writes don't reach the disk as one request per run. EXP-66's cost model is then wrong for the receiver, and idea 2's rule is reported but not applied.
- **Idea 2:** prototyped (with its own registration) only if the re-applied rule says so. Otherwise it is set aside with its reason recorded.
