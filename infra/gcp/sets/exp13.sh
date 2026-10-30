# EXP-13 (latency and transport): one pair shape for both sessions, so what differs between them is the day and the
# seeds, not the hardware. us-central1-a is EXP-13's registered zone, where EXP-09, EXP-11 and EXP-12 ran; an
# n2-standard-8 with four local NVMe SSDs in RAID 0 is EXP-11's pair, and EXP-13's preflight refuses to start
# without those four; Intel Cascade Lake is the platform EXP-11 ran on, set here as each VM's minimum so A19's
# comparison with EXP-11 holds (a VM that reports another platform is created again once, before provisioning).
# No client role: EXP-13 runs no workload, only transports over an emulated link.
ROLES=${ROLES:-source dest}
ZONE=${ZONE:-us-central1-a}
MACHINE=${MACHINE:-n2-standard-8}
DATA_DISK=${DATA_DISK:-local-nvme}
LOCAL_SSDS=${LOCAL_SSDS:-4}
MIN_CPU_PLATFORM=${MIN_CPU_PLATFORM:-"Intel Cascade Lake"}
# The one Debian 12 image both sessions boot from. Its exact name is resolved from the family once before session 1
#   gcloud compute images describe-from-family debian-12 --project debian-cloud --format='value(name)'
# and recorded, dated, in docs/experiments/EXP-13-latency-transport.md, then pinned here (or passed as IMAGE) for
# both sessions. While it is empty create.sh takes IMAGE_FAMILY's newest image, which is what the pin exists to
# prevent: session 2 would boot whatever the family holds on its day.
IMAGE=${IMAGE:-}
