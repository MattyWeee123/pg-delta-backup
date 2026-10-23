#!/bin/bash
# Create the two benchmark VMs. Costs money while they exist: run teardown.sh after.
#   ./create.sh            # create both VMs (skips ones that exist)
# Then wait for the startup script with:
#   python3 experiments/network/remote.py wait-ready
set -euo pipefail
cd "$(dirname "$0")"
source ./config.sh

gc() { "$GCLOUD" --project "$PROJECT" "$@"; }

# Read-only check: this script never enables APIs or changes project settings.
if ! gc services list --enabled --filter=config.name=compute.googleapis.com \
     --format='value(config.name)' | grep -q compute; then
  echo "Compute Engine API is not enabled in $PROJECT" >&2
  exit 1
fi

if [ ! -f "$SSH_KEY" ]; then
  mkdir -p "$(dirname "$SSH_KEY")"
  chmod 700 "$(dirname "$SSH_KEY")"
  ssh-keygen -q -t ed25519 -N '' -C "$SSH_USER@pgd-bench" -f "$SSH_KEY"
fi
pubkey=$(cat "$SSH_KEY.pub")

ssd_flags=()
for _ in $(seq "$LOCAL_SSDS"); do ssd_flags+=(--local-ssd=interface=NVME); done
created=0
for vm in "${VMS[@]}"; do
  if gc compute instances describe "$vm" --zone "$ZONE" >/dev/null 2>&1; then
    echo "$vm already exists"
    continue
  fi
  # No service account: the VMs need no Google API access, only the metadata
  # server, which works without one.
  gc compute instances create "$vm" --zone "$ZONE" --machine-type "$MACHINE" \
    --image-family "$IMAGE_FAMILY" --image-project "$IMAGE_PROJECT" \
    --boot-disk-size 50GB --boot-disk-type pd-balanced "${ssd_flags[@]}" \
    --no-service-account --no-scopes \
    --metadata "block-project-ssh-keys=TRUE,ssh-keys=$SSH_USER:$pubkey" \
    --metadata-from-file startup-script=startup.sh --labels "$LABELS"
  created=1
done
# Fresh VMs have fresh host keys; forget the old ones.
if [ "$created" = 1 ]; then rm -f "$SSH_KNOWN_HOSTS"; fi
gc compute instances list --filter="labels.purpose=pg-delta-benchmark" \
  --format="table(name,zone.basename(),machineType.basename(),networkInterfaces[0].networkIP,networkInterfaces[0].accessConfigs[0].natIP,status)"
