# EXP-88 stage 2 (beyond memory): the same arms and levels as stage 1, on a cluster that no longer fits in the
# source's page cache. 18 GB on 8 GB of RAM is beyond memory by 2.25x, and is EXP-48's and EXP-64's own shape, so
# stage 2's numbers sit beside theirs rather than opening a size nobody has measured.
#
# n2-highcpu-8 keeps 8 vCPUs (the load needs the CPU stage 1's load needed) with only 8 GB of RAM, and is in the
# N2 family, whose quota is free. Balanced persistent disk, not local NVMe: Omni's Kubernetes operator gives a
# node persistent disk, and EXP-57 found the checkpoint stall only once the disk was capped.
#
# STAGE 3 IS THIS SET WITH ONE CHANGE, and only one:
#   MACHINE=n2-standard-8   (8 vCPUs, 32 GB of RAM, so the same 18 GB fits in cache)
# Everything else - disk type and size, cluster, vCPUs, CPU platform, zone, image - is held, because stages 2
# and 3 exist to separate a page-cache effect that any other difference would confound.
ROLES=${ROLES:-source dest client}
ZONE=${ZONE:-us-east1-b}
MACHINE=${MACHINE:-n2-highcpu-8}
DATA_DISK=${DATA_DISK:-pd-balanced}
# 18 GB of cluster, plus B, plus pg_basebackup's output while a `full` run holds it, plus WAL and headroom.
PD_SIZE_GB=${PD_SIZE_GB:-200}
# Held identical between stages 2 and 3 so a different host generation cannot be read as the cache effect.
MIN_CPU_PLATFORM=${MIN_CPU_PLATFORM:-Intel Cascade Lake}
