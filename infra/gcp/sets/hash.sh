# EXP-29 (T5, lean): the per-core hash benchmark on both CPU families: an Intel n2-standard-8 (Cascade Lake, as
# every VM so far) and an AMD n2d-standard-8. No database: small balanced persistent disks.
ZONE=${ZONE:-us-central1-b}
MACHINE=${MACHINE:-n2-standard-8}
DEST_MACHINE=${DEST_MACHINE:-n2d-standard-8}
DATA_DISK=${DATA_DISK:-pd-balanced}
PD_SIZE_GB=${PD_SIZE_GB:-20}
