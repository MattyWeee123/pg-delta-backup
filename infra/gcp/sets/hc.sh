# The hc set (EXP-84): a second ha-like set (pd-balanced, n2d-highcpu-8), so a held-out setting (H4, PostgreSQL 18)
# can run beside the ha set's.
ZONE=${ZONE:-us-west1-b}
DEST_ZONE=${DEST_ZONE:-us-west1-c}
MACHINE=${MACHINE:-n2d-highcpu-8}
DATA_DISK=${DATA_DISK:-pd-balanced}
PD_SIZE_GB=${PD_SIZE_GB:-150}
