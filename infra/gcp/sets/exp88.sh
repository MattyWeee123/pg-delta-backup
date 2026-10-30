# EXP-88 stage 1 (the 2 GB pilot): v1 reading a live, loaded primary at three offered-load levels, with the
# load's client on its own VM. The same shape as EXP-28's t10b set, so the replication gate compares like with
# like: source and dest n2-standard-8 with local NVMe, the client an n2-standard-4.
#
# us-east1, not EXP-28's us-central1: pd-balanced counts against SSD_TOTAL_GB, and another session's disks hold
# 400 of us-central1's 500 GB, leaving less than this set's boot disks need. us-east1 has the full 500 GB free.
# The consequence is recorded in the registration: verdicts rest on within-session ratios and are unaffected;
# absolute times are not compared with EXP-28's, and are labelled "different region".
#
# Stages 2 and 3 are NOT this set. They need balanced persistent disk and 18 GB, and they vary RAM alone:
#   stage 2  MACHINE=n2-highcpu-8  (8 GB)   DATA_DISK=pd-balanced  PD_SIZE_GB=200
#   stage 3  MACHINE=n2-standard-8 (32 GB)  DATA_DISK=pd-balanced  PD_SIZE_GB=200
# Both pin MIN_CPU_PLATFORM to the same value as each other, so a different host generation cannot be mistaken
# for the page-cache effect those two stages exist to separate.
ROLES=${ROLES:-source dest client}
ZONE=${ZONE:-us-east1-b}
MACHINE=${MACHINE:-n2-standard-8}
DATA_DISK=${DATA_DISK:-local-nvme}
LOCAL_SSDS=${LOCAL_SSDS:-4}
# NOT YET EFFECTIVE: create.sh does not read MIN_CPU_PLATFORM on this branch, so this is recorded intent, not a
# pinned platform. It does not matter for stage 1 (one machine shape, and verdicts are within-session ratios),
# but it is load-bearing for stages 2 and 3, whose whole point is a page-cache contrast that a different host
# generation would confound. Port the create.sh option before stage 2 runs, then delete this comment.
MIN_CPU_PLATFORM=${MIN_CPU_PLATFORM:-Intel Cascade Lake}
