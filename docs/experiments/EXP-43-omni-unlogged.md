# EXP-43: Does v1 handle AlloyDB Omni's unlogged tables?

Status: **done October 26, 2026 (all five predictions held, in both runs).** Every copy started and behaved, but the registered rule fired: Omni's own `pg_basebackup` left out the unlogged table's `_mintxid` fork, and pgdelta copied it. So pgdelta now treats `_mintxid` as a fork (tested). Rerun with that fix, pgdelta's copies held what Omni's backup holds and passed again, a stopped-source copy without the fork included. Pre-registered before any run. OMNI-1c, lean: the open half of EXP-40's `_mintxid` question. One VM, about 15 minutes with set-up. The owner accepted the Omni container's license terms for this research use on October 26.

## Question

An unlogged table isn't WAL-logged, so a base backup can't carry its contents.
- **pg_basebackup** leaves out every fork of an unlogged relation except its `_init` fork, and recovery resets the relation to empty from that fork.
- **pgdelta's stopped-source mode** (`--source-kind datadir`) does the same by name, and sends the main fork with the `_init` fork's bytes, since its copy starts without recovery.

pgdelta's relation pattern doesn't know Omni's `_mintxid` fork (EXP-40), so for an unlogged table it would send that fork as S has it, beside a main fork reset to empty. Two questions:
- What does Omni's own `pg_basebackup` send of an unlogged table? Omni's server decides that, by its own copy of PostgreSQL's base-backup code.
- Does a B refreshed by v1, from a stopped and from a running Omni, start and behave with its unlogged table?

## Design

`experiments/omni/omni_unlogged.sh` on one `n2-standard-4` VM (`PAIR=omni`), Omni 17.10's container:
1. **On S:** a logged table `l` and an unlogged table `u`, each with a primary key and 200,000 rows. List each relation's forks.
2. **The reference:** Omni's own `pg_basebackup` (run in the container, `-X stream`); list what it sent of each relation.
3. **The changes:** B0 is S stopped and copied. S then updates and adds rows in both tables, and stops.
4. **The copies, each started in Omni and checked the same way:**
   - **the reference** (`pg_basebackup`'s copy);
   - **v1 in place from the stopped S;**
   - **v1 in place from S running,** with inserts into both tables while it reads.
5. **The checks:**
   - `u` is empty at start, as an unlogged table must be after a copy;
   - `l` matches S (stopped source only; while v1 reads a running S, more rows arrive);
   - `u` then takes 50,000 new rows;
   - `bt_index_check` with `heapallindexed` on every btree index, and `verify_heapam` on every table, unlogged included;
   - `VACUUM (FREEZE)`, then `verify_heapam` again.
6. **The analysis,** committed before the data: `analysis/exp43.py`.

## Predictions

1. Recorded: which forks `u`, `l` and their indexes have on S.
2. Recorded: whether Omni's `pg_basebackup` sends `u`'s `_mintxid` fork.
3. **Deciding: the B from the stopped S starts, `u` is empty and then takes 50,000 rows, `l` matches S, and every check is clean before and after `VACUUM (FREEZE)`.**
4. The B from the running S does the same, `l` aside.
5. The reference passes the same checks.

## What follows

- **If `pg_basebackup` leaves out `u`'s `_mintxid` fork and pgdelta sends it:** pgdelta's relation pattern learns the `_mintxid` fork, so that for an unlogged relation it is left out like the other forks. pgdelta then sends what Omni's own base backup sends, as it promises for PostgreSQL. This holds whether 3 passes or not, and the change is tested.
- **If 3 or 4 fails:** the cause is found before any Omni claim covers unlogged tables.
- **If all hold and the two copies match:** the question closes.

## Limitations

One unlogged table and one run of each kind; Omni in a container. What `_mintxid` holds is undocumented, so the checks test behaviour, not its contents.

## Results

Data: `docs/experiments/data/exp43-copies-before-fix.csv` (the registered run) and `exp43-copies.csv` (the rerun with the fix); raw `results/omni/exp43/` and `results/omni/exp43-fixed/`; analysis `analysis/exp43.py`, committed before the data. One `n2-standard-4` VM. us-central1-b and -c had no `n2-standard-2` for the set's idle second VM, so it ran as an `e2-standard-2`; the measured VM was unchanged.

### The forks

| Relation | Its forks on S |
|---|---|
| `l` (logged table) | main, `_fsm`, `_mintxid` |
| `l_pkey` | main, `_fsm` |
| `u` (unlogged table) | main, `_fsm`, `_init`, `_mintxid` |
| `u_pkey` | main, `_fsm`, `_init` |

As in EXP-42, tables have a `_mintxid` fork and indexes don't, now for unlogged tables too.

**What each copy holds of `u`:**

| Copy | `u`'s files |
|---|---|
| S | main (14.5 MiB), `_fsm`, `_init` (empty), `_mintxid` (32 KiB) |
| **Omni's own `pg_basebackup`** | **`_init` only** |
| v1 from the stopped S, as registered | `_init`, main (= `_init`), **`_mintxid` (32 KiB, S's)** |
| v1 from the running S, as registered | `_init`, **`_mintxid` (S's)** |
| v1 from the stopped S, with the fix | `_init`, main (= `_init`) |
| v1 from the running S, with the fix | `_init` only, as `pg_basebackup` |

pgdelta's relation pattern didn't know `_mintxid`, so the unlogged rule never matched it, and both modes copied S's fork beside a reset table.

### Every copy behaved, in both runs

| Check | `pg_basebackup` | v1 stopped | v1 running |
|---|---|---|---|
| Started in Omni | yes | yes (no recovery) | yes (recovery) |
| `u` empty at start | yes | yes | yes |
| `u` then takes 50,000 rows | yes | yes | yes |
| `l` as S | (taken earlier: 200,000 rows) | yes: 260,000 rows, same sum | (S kept writing: 285,000 rows) |
| 188 btree indexes (`bt_index_check`, `heapallindexed`) | clean | clean | clean |
| `verify_heapam`, before and after `VACUUM (FREEZE)` | 0 corruptions | 0 | 0 |

The fix's case worth checking held: a copy from a stopped S starts without recovery, and its unlogged table had no `_mintxid` file at all. Omni made one as rows went in.

| # | Prediction | Verdict |
|---|---|---|
| 1 | Recorded: the forks on S | Tables have `_mintxid`, indexes don't; unlogged ones also `_init` |
| 2 | Recorded: does Omni's `pg_basebackup` send `u`'s `_mintxid`? | **No:** only `_init` |
| 3 | **Deciding: the B from the stopped S starts and behaves, `l` as S** | **Held**, in both runs |
| 4 | The B from the running S does the same | **Held**, in both runs |
| 5 | The reference passes the same checks | **Held** |

## What follows

- **The registered rule fired, and the fix is in:** `pgdelta/datadir.py`'s relation patterns know `_mintxid`, so an unlogged relation's fork is left out like the others. That applies to stopped and live sources, and to temporary relations' forks. The tests pin each case: an unlogged relation's fork left out, a logged one's sent, a temporary one's left out. pgdelta now sends what Omni's own base backup sends, as it promises for PostgreSQL.
- **The registered behaviour held either way:** the copies were correct with S's stale fork too. So the fix aligns pgdelta with Omni's tool; it doesn't repair a failure seen here.
- **Omni's unlogged tables are covered** for the cases run here, stopped and live. EXP-40's last `_mintxid` question closes.
