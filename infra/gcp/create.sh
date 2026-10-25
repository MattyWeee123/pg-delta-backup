#!/bin/bash
# Create one set of benchmark VMs. Costs money while they exist: run teardown.sh after.
#   ./create.sh                     # the default set: pgd-source and pgd-dest (skips ones that exist)
#   PAIR=a ./create.sh              # the set named a: pgd-a-source and pgd-a-dest (sets/a.sh, if it exists)
#   PAIR=a DRY_RUN=1 ./create.sh    # print every gcloud command instead of running any
# config.sh holds the settings. Then wait for the startup script with:
#   python3 experiments/network/remote.py [--pair a] wait-ready
set -euo pipefail
cd "$(dirname "$0")"
source ./config.sh
source ./vmset.sh

pgd_check_create
pgd_print_set

# Read-only check: this script never enables APIs or changes project settings.
if ! gc_read compute.googleapis.com services list --enabled --filter=config.name=compute.googleapis.com \
     --format='value(config.name)' | grep -q compute; then
  echo "Compute Engine API is not enabled in $PROJECT" >&2
  exit 1
fi

if [ ! -f "$SSH_KEY" ] && [ "$DRY_RUN" = 1 ]; then
  echo "dry run: would create the key $SSH_KEY"
elif [ ! -f "$SSH_KEY" ]; then
  mkdir -p "$(dirname "$SSH_KEY")"
  chmod 700 "$(dirname "$SSH_KEY")"
  ssh-keygen -q -t ed25519 -N '' -C "$SSH_USER@pgd-bench" -f "$SSH_KEY"
fi
if [ -f "$SSH_KEY.pub" ]; then
  pubkey=$(cat "$SSH_KEY.pub")
else
  pubkey="ssh-ed25519 PUBLIC-KEY-OF-$SSH_KEY"  # a dry run without the key
fi

# What of the set exists already, in any zone. A role's VM that exists in its zone
# and belongs to the set is kept. One that is in another zone, or not the set's
# (another set's, or without the purpose label), stops the script before anything
# is made, so two sets never share a VM.
rows=$(pgd_list_instances)
owned=$(printf '%s\n' "$rows" | pgd_owned_instances)
missing=()
conflicts=
for role in $ROLES; do
  vm=$(pgd_role "$role" VM)
  zone=$(pgd_role "$role" ZONE)
  found=$(printf '%s\n' "$rows" | grep "^$vm," || true)
  if [ -z "$found" ]; then
    missing+=("$role")
    continue
  fi
  while IFS=, read -r name where set purpose; do
    if ! printf '%s\n' "$owned" | grep -qx "$name,$where"; then
      conflicts="$conflicts
  $name in $where is not set $SET_NAME's (set '${set:-none}', purpose '${purpose:-none}')"
    elif [ "$where" != "$zone" ]; then
      conflicts="$conflicts
  $name is in $where; set $SET_NAME asks for $zone"
    else
      echo "$vm already exists in $zone"
    fi
  done <<< "$found"
done
if [ -n "$conflicts" ]; then
  echo "create.sh: nothing created:$conflicts" >&2
  exit 1
fi
for row in $owned; do
  case " ${VMS[*]} " in
    *" ${row%%,*} "*) ;;
    *) echo "note: ${row%%,*} (${row#*,}) also belongs to set $SET_NAME; teardown.sh deletes it too" ;;
  esac
done

pgd_quota_check ${missing[@]+"${missing[@]}"}

created=0
for role in ${missing[@]+"${missing[@]}"}; do
  vm=$(pgd_role "$role" VM)
  disk=$(pgd_role "$role" DATA_DISK)
  disk_flags=()
  case $disk in
    local-nvme)
      for _ in $(seq "$(pgd_role "$role" LOCAL_SSDS)"); do disk_flags+=(--local-ssd=interface=NVME); done ;;
    pd-balanced)
      # Attached as pgd-data (startup.sh finds it at /dev/disk/by-id/google-pgd-data), deleted with the VM.
      disk_flags+=("--create-disk=name=$vm-data,size=$(pgd_role "$role" PD_SIZE_GB)GB,type=pd-balanced,device-name=pgd-data,auto-delete=yes,labels=$(pgd_labels "$role" | tr , :)") ;;
  esac
  # No service account: the VMs need no Google API access, only the metadata
  # server, which works without one. The metadata's pgd-* keys say which set and
  # role the VM is (experiments/network/agent.py env records them).
  gc compute instances create "$vm" --zone "$(pgd_role "$role" ZONE)" --machine-type "$(pgd_role "$role" MACHINE)" \
    --image-family "$IMAGE_FAMILY" --image-project "$IMAGE_PROJECT" \
    --boot-disk-size 50GB --boot-disk-type pd-balanced ${disk_flags[@]+"${disk_flags[@]}"} \
    --no-service-account --no-scopes \
    --metadata "block-project-ssh-keys=TRUE,ssh-keys=$SSH_USER:$pubkey,pgd-set=$SET_NAME,pgd-role=$role,pgd-data-disk=$disk" \
    --metadata-from-file startup-script=startup.sh --labels "$(pgd_labels "$role")"
  created=1
done
# Fresh VMs have fresh host keys; forget the set's old ones.
if [ "$created" = 1 ]; then pgd_forget_hosts; fi
gc compute instances list --filter="labels.purpose=pg-delta-benchmark" \
  --format="table(name,labels.pgd-set:label=SET,zone.basename(),machineType.basename(),networkInterfaces[0].networkIP,networkInterfaces[0].accessConfigs[0].natIP,status)"
