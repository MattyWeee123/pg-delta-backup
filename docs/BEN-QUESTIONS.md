# Questions for Ben

Ask the first five before committing to the production architecture.
The defaults below let the team start experiments; they are not sponsor approvals.

| Priority | Question | Why the answer matters | Proposed temporary default |
|---|---|---|---|
| P0 | What customer operation are we optimizing: refresh an old backup, reseed a stopped replica, recover a diverged node, or something else? What exactly exists at the destination? | Defines input validity, required history, and whether native incremental or pg_rewind already fits | An immutable completed backup from the same cluster |
| P0 | What is the expected advantage over PostgreSQL 17+ incremental backup, rsync, and pgBackRest? Is operation without old WAL summaries the intended distinction? | Defines a useful contribution and the correct comparison | Treat history-independent content matching as a hypothesis |
| P0 | May we run a helper on the source host or in a sidecar with local backup access? Are server patches allowed? Is a full source-side staging backup acceptable for the first demo? | A destination-only full-backup client receives bytes before it can discard them; source-side work is needed to save that link's traffic | Stage a standard backup on the source side for the first experiment |
| P0 | Which exact PostgreSQL major/minor/build and AlloyDB Omni deployment/version must we support? Is the target Omni or the managed AlloyDB service? | Determines available APIs, binaries, permissions, and compatibility tests | PostgreSQL 17 research baseline; confirm Omni independently |
| P0 | Must the final tool capture a live primary, a standby, or only an already completed backup? Are failover and tablespaces required? | Separates a delta copier from a live-backup product | Completed backup first; live source is a gated later milestone |
| P1 | What metric is primary: end-to-end refresh time, bytes across a particular link, source I/O, CPU, or replica catch-up time? What data size, churn, and network rate represent success? | Prevents optimizing a metric the sponsor does not value | Correctness gate, then link bytes and total time; report all costs |
| P1 | Is content-defined chunking a required deliverable, or should it be compared experimentally with fixed page-aligned chunks? | Prevents selecting the algorithm before measuring the workload | Fixed chunks first, CDC as a controlled comparison |
| P1 | Is a standalone Rust implementation acceptable? Is upstream PostgreSQL contribution expected this semester, implying C and backend work? | Changes language and integration scope | Rust core if standalone is accepted; Python only for experiments |
| P1 | What repo owner, visibility, license, CLA requirements, and source-sharing restrictions apply? Can we publish sponsor-derived specifications? | Needed before public release or an upstream submission | Private team repo, no license selected, no original meeting-note upload |
| P1 | What are the cloud budget, approved instance sizes, reviewer availability, and exact demo date? | Defines experiment scale and schedule | Small synthetic data; no cloud provisioning until budget is known |

## Draft message for Matt to review and send

Ben, we are turning the initial proposal into implementation tasks.
Our current interpretation is a standalone tool that reuses an older destination copy through content matching and transfers only missing bytes.
We found that PostgreSQL 17+ already supports WAL-summary-based incremental backup, and that rsync and pgBackRest are relevant comparisons.
Could you confirm the intended use case and the advantage you most want us to demonstrate?

The architecture depends especially on whether we may run a source-side helper, whether a source-side staged pg_basebackup is acceptable for the first milestone, and which PostgreSQL/Omni versions and deployment model we should target.
We propose starting with a completed backup, reconstructing it from an older copy, and proving a successful restore before extending the live-source path.
Does that first milestone fit your expectations?
We would also like to confirm the primary performance metric, repository/licensing expectations, and final demo date.

This is a draft only; no message has been sent.
