# EXP-93 (what the hashing campaign's speed cost the source's load): EXP-88 stage 1's pair and client, under
# its own name.
#
# The shape is not restated here, it is sourced. EXP-93's whole design is "EXP-88 stage 1's exact shape", and
# its replication anchor is stage 1's own `v1`/L60 cell: three VMs in us-east1-b, source and dest
# n2-standard-8 with four local NVMe SSDs, the client an n2-standard-4, a 2 GB cluster on 32 GB of RAM.
# Sourcing the file stage 1 ran is the only way to say that and keep it true when exp88.sh changes.
#
# Note carried forward from exp88.sh, because it bears on EXP-93's own absolute numbers: stage 1's VMs were
# created before create.sh read MIN_CPU_PLATFORM, so stage 1 ran on an unpinned host generation while this
# session will run on a pinned one. EXP-93's verdicts are within-session ratios against `pg_basebackup` under
# the same load, which that does not touch; a comparison of absolute seconds with stage 1's is labelled
# "platform changed".
source "$PGD_SETS_DIR/exp88.sh"

# What this set adds is its own name, so the VMs are pgd-exp93-source, -dest and -client and teardown.sh
# deletes only this session's three, and a run cap.
#
# The session is 36 runs plus set-up: C measured on an unloaded source, a 2 GB cluster built once, then two
# load blocks. Comparable to stage 1's 39 runs, which took about 25 minutes of scored window inside a session
# of roughly 2 hours. 3 hours is generous for that and well short of the 8-hour default. Compute Engine
# enforces it server-side (create.sh's --max-run-duration with an --instance-termination-action), so a
# forgotten set cannot bill for days even when no local credential can reach the API - the failure this cap
# exists for, after a stage-2 set once billed about 57 hours instead of 2.
#
# These are local-SSD VMs, so Compute Engine refuses DELETE as the action and the cap STOPs them instead: that
# ends compute billing but loses /data. Fetch the results before walking away.
MAX_RUN_HOURS=${MAX_RUN_HOURS:-3}
