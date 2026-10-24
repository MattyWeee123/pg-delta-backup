-- EXP-12 (T10a): the load's committed transaction, weight 99. Each adds exactly 1 to the
-- sum of abalance and 1 to the client's n, so in any consistent state the two sums are equal. Locks
-- are taken in one order (lower account, higher account, the client's own row): no deadlocks.
\set a1 random(1, 200000)
\set a2 random(1, 200000)
\set lo least(:a1, :a2)
\set hi greatest(:a1, :a2)
\set d random(2, 5000)
BEGIN;
UPDATE pgbench_accounts SET abalance = abalance + :d WHERE aid = :lo;
UPDATE pgbench_accounts SET abalance = abalance - :d + 1 WHERE aid = :hi;
UPDATE load_progress SET n = n + 1 WHERE client = :client_id;
END;
