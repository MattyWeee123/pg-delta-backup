# EXP-88 stage 3 (the page-cache control): stage 2's set with RAM changed and NOTHING else.
#
# n2-standard-8 is 8 vCPUs with 32 GB, against stage 2's n2-highcpu-8 at 8 vCPUs with 8 GB. Same vCPU count,
# same balanced persistent disk at the same 150 GB, same 18 GB cluster, same pinned CPU platform, same zone and
# image. RAM is the only variable, which is what makes a difference between the two stages attributable to what
# fits in the page cache rather than to the machine.
#
# Deliberately NOT shrunk to save money. pd-balanced's throughput scales with provisioned size, so a smaller
# disk is a slower disk; fewer vCPUs would change the load's own headroom. Either would confound the single
# comparison this stage exists to make, and the saving would be about a dollar.
ROLES=${ROLES:-source dest client}
ZONE=${ZONE:-us-east1-b}
MACHINE=${MACHINE:-n2-standard-8}
DATA_DISK=${DATA_DISK:-pd-balanced}
PD_SIZE_GB=${PD_SIZE_GB:-150}
MIN_CPU_PLATFORM=${MIN_CPU_PLATFORM:-Intel Cascade Lake}
