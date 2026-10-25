# EXP-26: BBR's fairness on a shared bottleneck. Two VMs in one zone, the same machine type as EXP-20/24's pair; the
# test moves no database, so a small balanced persistent disk instead of local SSDs (which stock out).
ZONE=${ZONE:-us-central1-b}
MACHINE=${MACHINE:-n2-standard-8}
DATA_DISK=${DATA_DISK:-pd-balanced}
PD_SIZE_GB=${PD_SIZE_GB:-50}
