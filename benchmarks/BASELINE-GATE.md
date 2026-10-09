# What the benchmark must establish

## Sponsor objective and current status

The project lead supplied Ben Tran's meeting comment on October 9, 2026. It asks the team to define an active-workload benchmark comparing pg_basebackup against the team's eventual tool on backup time, network traffic, and application performance degradation across team-defined scales. It also asks for a concrete week-by-week plan. This is project context, not an instruction to modify the linked meeting document or contact teammates.

The comment supports the existing repository direction. It does not prescribe GCP, a managed AlloyDB instance, a specific workload, or numerical scale definitions. Those are engineering decisions. The native runner is the first executable step, not completion of that deliverable.

The research question is whether reusing an older backup at the destination reduces the cost of producing a correct new backup. A smaller output alone does not answer that question: reading, hashing, capture, transfer, reconstruction, verification and database interference all have costs.

## Comparisons and why they are needed

| Treatment | Question it answers | Current implementation |
|---|---|---|
| Active workload, no measured backup | What is normal application throughput and latency? | Smoke control exists; matched-window degradation calculation remains pending. |
| Full pg_basebackup | What does the standard complete backup cost? | Local capture, verification and isolated restore run. |
| Native incremental plus pg_combinebackup | How well does PostgreSQL's existing block-change solution do? | Local capture, input verification, combination, final verification and restore run. |
| Full transfer and rsync of the same frozen backup | Is a custom transfer algorithm better than copying everything or an existing delta tool? | Planned; must use separate measured transport processes and immutable inputs. |
| Source capture plus the custom tool | Does our complete approach improve Ben's metrics? | Not implemented. Source staging belongs inside the end-to-end cost. |

PostgreSQL 17's incremental backup needs an earlier backup and WAL summaries. Its incremental artifact must be combined before it can be restored. Include that reconstruction work in a comparison of usable backups; retain capture-only timing as a separate diagnostic. See [PostgreSQL's documentation](https://www.postgresql.org/docs/17/app-pgbasebackup.html).

Rsync and the custom transfer component operate on completed, frozen backups. Applying ordinary file synchronization directly to changing live database files is outside this design. The integrated experiment still captures a new backup while database clients are active.

## Tests versus measurements

Correctness is a pass/fail gate: verify files and required recovery logs, actually start a restored database, and compare its logical contents against an independently exported expected state. Include inserts, deletes, updates and rollback, rather than checking only row counts. Reject intentionally damaged, incomplete and outdated backups to test the checker itself. Unit tests exercise the transaction-log parser and evidence handling so missing observations do not become false successes or zero-cost results.

The current active test checks package-only startup and balance conservation, then uses separately recorded archived WAL to restore to an agreed named point and compare all fixture data and supported schema. This latter check is PITR-assisted. Exact contents at the standalone active backup's own recovery cutoff remain a release gap; successful later recovery must not conceal that distinction.

Performance measurements answer a different question. Collect capture time, time to a verified usable backup, restore time, measured traffic in both directions, completed transactions per second, latency percentiles, queueing and failures. Source/destination CPU, disk activity, memory and scratch space help explain bottlenecks. Stored file bytes are not wire bytes. Never replace unavailable measurements with zero.

The smoke runner records local durations, file sizes and transaction logs. It deliberately has no performance pass threshold on a hosted CI machine. Fifteen small successful trials establish that these paths execute; they do not establish a speedup or quantify application degradation.

## Workload and scale proposal

Start with the deterministic account-transfer fixture because every committed transaction has a simple invariant: balances change but their total stays constant. An audit row in the same transaction, reconciled against external successful transaction logs, adds another check. This is a synthetic OLTP-like workload, not TPC-C and not a NOPM benchmark. Later add a declared read/write mix and localized versus distributed changes to avoid tuning for a single convenient fixture.

Keep size, change amount and application load separate. Initial scale proposals are approximately 1 GiB, 10 GiB and 50 GiB of measured cluster data. These are not sponsor requirements; the 50 GiB case is conditional on space and budget. The present 20,000-row fixture is much smaller and is labeled smoke, not small-scale performance.

First calibrate sustainable no-backup transaction rate on the chosen hardware. Then freeze offered loads near 25%, 50% and 75% of that measured rate, subject to a declared latency and failure criterion. At a fixed offered rate, a backup may increase latency while TPS barely changes. Retain scheduling lag and unsuccessful requests. Test 0%, 1%, 10% and 100% changes to a defined row set separately; row-change fraction is not the same as changed physical-block fraction.

Pilot one size, one moderate load and one change level before expanding. Select a compact primary matrix and additional sensitivity cases instead of starting with every possible combination. Match no-backup observation windows to backup runs, record cache policy, exclude a declared warmup, randomize method order, and retain at least three measured repetitions with raw spread. Increase repetitions if the pilot cannot separate variation from the effect being measured.

## Environment and cost

GCP can supply a shared source machine, destination machine, controlled network and reproducible disks for later experiments. It is a place to run the lab, not the benchmark itself. A suitable existing isolated Linux machine can also qualify the harness. Hosted CI is useful for low-cost correctness checks; its hardware variation and current single-container topology do not establish a controlled cross-host performance baseline.

No GCP resources are created by this runner. The project lead reports unresolved sponsor billing and personal charges. Keep cloud provisioning deferred until funding and a bounded spending allowance are settled. A managed AlloyDB deployment is not assumed to support the physical-file experiment; validate AlloyDB Omni separately on an agreed build if required.

## Completion gate before optimization

1. Pin software and document hardware, storage, network, resources and durability settings.
2. Complete the declared active recovery oracle and rejection controls.
3. Add the transfer/staging baselines, actual traffic counters and resource observers; validate observer overhead with known-size transfers.
4. Calibrate the workload and freeze scales, change patterns, timing boundaries and matched controls before measuring.
5. Run the chosen baseline matrix, retain every trial and publish reproducible raw evidence and charts with uncertainty limits.
6. Keep the evaluator fixed when adding optimization candidates; require correctness first and use fresh matched baseline runs and reserved workloads for improvement claims.

Full, incremental and no-backup reference results can be completed before the custom tool exists. The final sponsor comparison necessarily comes later, once that tool is runnable.

## Proposed relative weeks

| Week | Reviewable outcome |
|---|---|
| 1 | Native full/incremental/restore smoke harness and evidence; settle recovery contract and machine choice. |
| 2 | Measured transport, rsync/full-transfer controls, active oracle and resource observers; pilot runtime and space. |
| 3 | Frozen primary matrix, repeatable native/transfer baseline runs, documented results and charts. |
| 4 onward | Implement the simplest correct custom path; compare it with the frozen baselines, then profile and optimize. |

These are proposed sequencing targets, not teammate assignments or promised calendar deadlines. Large-scale and Omni work follow demonstrated feasibility and funding.
