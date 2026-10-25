# EXP-34 (T13c, lean): the in-place refresh under a power loss. One VM (revised before any trial: the SSD quota left
# room for one 50 GB boot disk and a 40 GB data disk while EXP-31's pair ran). The source cluster, stopped and only
# read, sits on it too, and `pgdelta local --in-place` runs both ends; the VM is hard-reset mid-refresh, so its data
# must be on a persistent disk, which keeps its filesystem across a reset.
ROLES=${ROLES:-dest}
ZONE=${ZONE:-us-central1-b}
MACHINE=${MACHINE:-n2-standard-4}
DATA_DISK=${DATA_DISK:-pd-balanced}
PD_SIZE_GB=${PD_SIZE_GB:-40}
