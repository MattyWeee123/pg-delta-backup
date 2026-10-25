# EXP-22: a scale model of Omni's HA rebuild: source and dest in two zones, balanced persistent disks of 500 GiB
# (about 280 MiB/s by Google's formula), and 8 GB of memory per VM so a 12 GB database can't stay in page cache.
ZONE=${ZONE:-us-central1-a}
DEST_ZONE=${DEST_ZONE:-us-central1-b}
MACHINE=${MACHINE:-n2-highcpu-8}
DATA_DISK=${DATA_DISK:-pd-balanced}
PD_SIZE_GB=${PD_SIZE_GB:-500}
