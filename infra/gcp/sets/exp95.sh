# EXP-95 (is EXP-94's DR win hashing ahead or just reading ahead): EXP-94's pair, under its own name.
#
# The shape is sourced, not restated. This session's predictions 4 and 5 compare its `_ha` arm and its `full`
# with EXP-94's, and both comparisons are meaningless if the hardware moved: the zone, the machine type, the
# four local NVMe SSDs in RAID 0 and Intel Cascade Lake as the minimum platform all have to be what EXP-94 had.
# Sourcing the file EXP-94 used (through dr94.sh, which sources this same exp13.sh) is the only way to say that
# and keep it true when exp13.sh changes.
source "$PGD_SETS_DIR/exp13.sh"

# Pinned to the image EXP-94 ran, resolved from the debian-12 family on 6 November and recorded in that
# experiment's results. Without the pin, create.sh takes the family's newest, which would make prediction 5's
# 1% gate a comparison across two images.
IMAGE=${IMAGE:-debian-12-bookworm-v20261102}

# 48 scored runs plus an 8-run smoke, about 25 minutes of runs; EXP-94 measured 24.6 s a run on this shape.
# Two hours is generous for that and a quarter of the 8-hour default. Compute Engine enforces it server-side
# (create.sh's --max-run-duration with an --instance-termination-action), so a forgotten pair cannot bill for
# days even when no local credential can reach the API - which is the failure this cap exists for, after a
# stage-2 set once ran about 57 hours instead of 2, and after EXP-94's own first attempt was stopped by it.
#
# These are local-SSD VMs, so Compute Engine refuses DELETE as the action and the cap STOPs them instead: that
# ends compute billing but loses /data. Fetch the results before walking away.
MAX_RUN_HOURS=${MAX_RUN_HOURS:-2}
