# The hn set (EXP-87, held-out setting H7): the ha set's VMs with local NVMe SSDs for /data (two, striped) instead
# of a persistent disk.
ZONE=${ZONE:-us-central1-a}
DEST_ZONE=${DEST_ZONE:-us-central1-b}
MACHINE=${MACHINE:-n2d-highcpu-8}
DATA_DISK=${DATA_DISK:-local-nvme}
LOCAL_SSDS=${LOCAL_SSDS:-2}
