# EXP-94 (does the sender keeping its stream full remove v1's need for BBR at DR round trips): the pair EXP-20
# and EXP-24 ran on, under its own name.
#
# The shape is not restated here, it is sourced. EXP-94's prediction 2 is a rule-17 comparability gate against
# EXP-24's `full` in the same cells, and that gate means nothing if the hardware moved between the sessions: the
# zone, the machine type, the four local NVMe SSDs in RAID 0 and Intel Cascade Lake as the minimum platform all
# have to be what those sessions had. Sourcing the file they used is the only way to say that and keep it true
# when exp13.sh changes.
#
# What this set adds is its own name, so the VMs are pgd-dr94-source and pgd-dr94-dest and teardown.sh deletes
# only this session's pair, and a tighter run cap.
source "$PGD_SETS_DIR/exp13.sh"

# The session is a smoke plus 180 runs, about 90 minutes, plus provisioning. 4 hours is generous for that and
# still far short of the 8-hour default. Compute Engine enforces it server-side (create.sh's
# --max-run-duration with an --instance-termination-action), so a forgotten pair cannot bill for days even when
# no local credential can reach the API - which is the failure this cap exists for: a stage-2 set once ran about
# 57 hours instead of 2.
#
# Note what the termination does to a local-SSD VM: Compute Engine refuses DELETE for those, so the action is
# STOP, which ends compute billing but loses the local SSDs' contents. Fetch the results before walking away.
MAX_RUN_HOURS=${MAX_RUN_HOURS:-4}
