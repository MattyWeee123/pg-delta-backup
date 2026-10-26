# EXP-40 (OMNI-1, lean): AlloyDB Omni's container on one VM (the source role); the dest role is a small idle VM,
# since a set needs both. The owner accepted the Omni container's license terms for this research use (October 26).
ZONE=${ZONE:-us-central1-b}
MACHINE=${MACHINE:-n2-standard-4}
DEST_MACHINE=${DEST_MACHINE:-n2-standard-2}
DATA_DISK=${DATA_DISK:-pd-balanced}
PD_SIZE_GB=${PD_SIZE_GB:-50}
