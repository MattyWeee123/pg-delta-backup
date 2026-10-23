# Settings for the two-machine benchmark (EXP-04). Sourced by the other scripts
# and read by experiments/network/remote.py. Every value can be overridden from
# the environment.
PROJECT=${PROJECT:-postgres-delta-backup}
# us-central1-a, -c and -f had no n2-standard-8 with local SSDs on 2026-10-22
# (stockout); -b had them with 4 SSDs but not with 2.
ZONE=${ZONE:-us-central1-b}
# n2-standard-8: 8 vCPUs, 32 GB RAM, up to 16 Gbps egress.
MACHINE=${MACHINE:-n2-standard-8}
# Local NVMe SSDs per VM (375 GB each), striped (RAID 0) at /data, so disks are
# not the bottleneck when the network is throttled. The design asked for 2; 4
# were what the zone could supply. Local SSD data is lost when a VM stops, which
# is fine for disposable benchmark clusters.
LOCAL_SSDS=${LOCAL_SSDS:-4}
IMAGE_FAMILY=${IMAGE_FAMILY:-debian-12}
IMAGE_PROJECT=${IMAGE_PROJECT:-debian-cloud}
SOURCE_VM=${SOURCE_VM:-pgd-source}
DEST_VM=${DEST_VM:-pgd-dest}
VMS=("$SOURCE_VM" "$DEST_VM")
LABELS=${LABELS:-purpose=pg-delta-benchmark,owner=pg-delta-backup}
GCLOUD=${GCLOUD:-$(command -v gcloud || echo /opt/homebrew/share/google-cloud-sdk/bin/gcloud)}
# Login user created on both VMs from instance metadata, and the Mac-side key
# for it. The key is put in instance metadata only (block-project-ssh-keys), so
# the project's metadata is never touched.
SSH_USER=${SSH_USER:-bench}
SSH_KEY=${SSH_KEY:-$HOME/.ssh/pgd-bench/id_ed25519}
SSH_KNOWN_HOSTS=${SSH_KNOWN_HOSTS:-$(dirname "$SSH_KEY")/known_hosts}
