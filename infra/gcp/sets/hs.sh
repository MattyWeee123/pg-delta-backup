# The hs set (EXP-84, held-out setting H3): the ha set's VMs with a pd-ssd data disk instead of pd-balanced.
ZONE=${ZONE:-us-west1-b}
DEST_ZONE=${DEST_ZONE:-us-west1-c}
MACHINE=${MACHINE:-n2d-highcpu-8}
DATA_DISK=${DATA_DISK:-pd-ssd}
PD_SIZE_GB=${PD_SIZE_GB:-150}
