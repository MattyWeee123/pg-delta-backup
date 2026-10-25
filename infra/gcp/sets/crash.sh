# EXP-34 (T13c, lean): the in-place refresh under a power loss. The destination is hard-reset (gcloud compute
# instances reset) mid-refresh, so its data must be on a persistent disk, which keeps its filesystem across a reset.
ZONE=${ZONE:-us-central1-b}
MACHINE=${MACHINE:-n2-standard-4}
DATA_DISK=${DATA_DISK:-pd-balanced}
PD_SIZE_GB=${PD_SIZE_GB:-100}
