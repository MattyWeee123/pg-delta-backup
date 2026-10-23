#!/bin/bash
# Delete the benchmark VMs (and their local SSDs and boot disks). Fetch results
# first: everything under /data is lost.
set -euo pipefail
cd "$(dirname "$0")"
source ./config.sh

gc() { "$GCLOUD" --project "$PROJECT" "$@"; }
existing=()
for vm in "${VMS[@]}"; do
  if gc compute instances describe "$vm" --zone "$ZONE" >/dev/null 2>&1; then existing+=("$vm"); fi
done
if [ ${#existing[@]} -gt 0 ]; then
  gc compute instances delete "${existing[@]}" --zone "$ZONE" --quiet
fi
gc compute instances list --filter="labels.purpose=pg-delta-benchmark"
