# The hm set (EXP-87, held-out setting H6): the ha set's disks on n2d-standard-8 (32 GB of memory), so the database
# fits in memory.
ZONE=${ZONE:-us-central1-a}
DEST_ZONE=${DEST_ZONE:-us-central1-b}
MACHINE=${MACHINE:-n2d-standard-8}
DATA_DISK=${DATA_DISK:-pd-balanced}
PD_SIZE_GB=${PD_SIZE_GB:-150}
