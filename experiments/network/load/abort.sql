-- EXP-12 (T10a): the load's rolled-back transaction, weight 1. A change of 10^9 that must never
-- reach a copy (G3: "aborted/uncommitted effects are absent").
\set a random(1, 200000)
BEGIN;
UPDATE pgbench_accounts SET abalance = abalance + 1000000000 WHERE aid = :a;
ROLLBACK;
