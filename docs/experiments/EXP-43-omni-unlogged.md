# EXP-43: Does v1 handle AlloyDB Omni's unlogged tables?

Status: **pre-registered October 26, 2026, before any run.** OMNI-1c, lean: the open half of EXP-40's `_mintxid` question. One VM, about 15 minutes with set-up. The owner accepted the Omni container's license terms for this research use on October 26.

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
