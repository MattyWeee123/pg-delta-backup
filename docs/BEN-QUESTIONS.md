# Remaining questions and proposal for Ben

Updated September 30, 2026 after the supplied Google Chat screenshots.

## Already answered in the chats

- Benchmark while a database workload is active.
- Compare time, network traffic and performance degradation against pg_basebackup.
- Define workload scales and feasible mid-semester/final deliverables as a team.
- Provide recovery correctness and no data loss; merely starting PostgreSQL is insufficient.
- Do not require byte identity with a separate pg_basebackup capture, even if finish times match.
- Build a project-specific correctness benchmark; Sam offered to research validation.

Ben endorsed the direction of logical equivalence and appropriate recovery guarantees.
The screenshots do not establish a precise recovery cutoff, oracle implementation, source deployment model or target software version.

## Choices for the team to propose

Use CORRECTNESS.md to define recovery coverage and freshness.
Propose approximately 1 GiB, 10 GiB and conditionally 50 GiB data sets, recording actual size and adapting to the approved budget.
Report all three requested outcomes: backup duration, traffic and application degradation.
Use a frozen-copy reconstruction as an internal milestone toward the active-workload demonstration.
The team can begin the native baseline and correctness harness now.

## Questions that still change the design

| Priority | Question | Why the answer matters | Proposed temporary default |
|---|---|---|---|
| P0 | What customer operation are we optimizing: refresh an old backup, reseed a stopped replica, recover a diverged node, or something else? What exactly exists at the destination? | Defines input validity, required history, and whether native incremental or pg_rewind already fits | An immutable completed backup from the same cluster |
| P1 | Is there a customer scenario where native incremental prerequisites are unavailable, or another differentiator we should test? | Shapes the comparison without redefining the confirmed deliverable | Treat history-independent content matching as one hypothesis |
| P0 | May we run a helper on the source host or sidecar? Are server patches allowed, and are there source scratch-storage limits? | Determines where matching can run and whether source staging is deployable | Stage a standard backup for the internal experiment; confirm deployment constraints |
| P0 | Which exact PostgreSQL major/minor/build and AlloyDB Omni deployment/version must we support? Is the target Omni or the managed AlloyDB service? | Determines available APIs, binaries, permissions, and compatibility tests | PostgreSQL 17 research baseline; confirm Omni independently |
| P0 | For the required live-workload experiment, should we support primary only, or also standby capture, failover and tablespaces? | Defines supported capture cases | Primary first; reject unsupported cases |
| P1 | Does an explicit recovery cutoff plus reported freshness match the intended no-data-loss guarantee, or is a stronger completion-time guarantee required? | Distinguishes captured database state from later command completion | Preserve committed transactional state through the declared cutoff |
| P1 | Is upstream PostgreSQL contribution expected this semester? | Determines whether C/backend work is required | Team selects a standalone language unless integration imposes a constraint |
| P1 | What repo owner, visibility, license, CLA requirements, and source-sharing restrictions apply? Can we publish sponsor-derived specifications? | Needed before public release or an upstream submission | Private team repo, no license selected, no original meeting-note upload |
| P1 | What are the cloud budget, approved instance sizes, reviewer availability, and exact demo date? | Defines experiment scale and schedule | Small synthetic data; no cloud provisioning until budget is known |

## Draft message for Matt to review and send

Ben, we have translated the chat discussion into a proposed benchmark and correctness contract.
We will measure backup duration, traffic and workload degradation across defined scales, and validate committed data at a recorded recovery point rather than compare independent backups byte for byte.
We will include a no-backup workload run and relevant existing incremental/delta baselines.

Our first internal milestone uses standard source-side capture followed by delta reconstruction from an older immutable backup.
We will include capture and staging costs in the end-to-end measurements and progress to the active-workload demonstration.
The deployment questions are whether a source-side helper is allowed, what the destination copy consists of, and which PostgreSQL/Omni versions we should target.
Please flag any customer or guarantee expectations that change our proposed recovery cutoff, scales or milestones, along with repository/licensing and budget constraints.

This is a draft only; no message has been sent.
