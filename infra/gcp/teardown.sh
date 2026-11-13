#!/bin/bash
# Delete one set of benchmark VMs with all their disks (boot, local SSDs and the
# pd-balanced data disk), then check that nothing of the set is left. Fetch
# results first: everything under /data is lost. Other sets are never touched.
#   ./teardown.sh                    # the default set: pgd-source and pgd-dest
#   PAIR=a ./teardown.sh             # the set named a
#   PAIR=a DRY_RUN=1 ./teardown.sh   # print every gcloud command instead of running any
# A VM belongs to the set when it is labelled purpose=pg-delta-benchmark and
# pgd-set=<the set>, or, made before sets had labels, has no set label and one of
# the set's names. Its disks are found by the same labels or by its name.
set -euo pipefail
cd "$(dirname "$0")"
source ./config.sh
source ./vmset.sh

echo "set $SET_NAME: deleting its VMs (${VMS[*]}) and their disks"
rows=$(pgd_list_instances "$(pgd_assumed_instances)")
owned=$(printf '%s\n' "$rows" | pgd_owned_instances)
# A VM with one of the set's names that isn't the set's is left alone, and said so.
for vm in "${VMS[@]}"; do
  for row in $(printf '%s\n' "$rows" | grep "^$vm," || true); do
    printf '%s\n' "$owned" | grep -qx "$vm,$(printf '%s' "$row" | cut -d, -f2)" \
      || echo "left alone: $row (not set $SET_NAME's: name,zone,set,purpose)" >&2
  done
done

deleted=()
for zone in $(printf '%s\n' "$owned" | cut -d, -f2 | sort -u); do
  names=$(printf '%s\n' "$owned" | grep ",$zone\$" | cut -d, -f1)
  # --delete-disks=all: every disk attached to them goes too, whatever its auto-delete.
  gc compute instances delete $names --zone "$zone" --delete-disks=all --quiet
  deleted+=($names)
done

# Disks the VMs left: a data disk whose VM failed to start, a boot disk kept by
# hand. One still attached isn't deleted; the check below reports it.
set_vms=$(printf '%s\n' "$owned" | cut -d, -f1 | tr '\n' ' ')
disk_names=$(pgd_disk_names ${deleted[@]+"${deleted[@]}"})
disks=$(pgd_list_disks '' ${deleted[@]+"${deleted[@]}"} | pgd_owned_disks "$set_vms" $disk_names)
for zone in $(printf '%s\n' "$disks" | sed '/^$/d' | cut -d, -f2 | sort -u); do
  names=$(printf '%s\n' "$disks" | awk -F, -v zone="$zone" '$2 == zone && $3 == "" {print $1}')
  [ -z "$names" ] || gc compute disks delete $names --zone "$zone" --quiet
done

# Nothing of the set may be left.
left=$({
  pgd_list_instances | pgd_owned_instances | sed 's/^/  VM /'
  pgd_list_disks '' ${deleted[@]+"${deleted[@]}"} | pgd_owned_disks "$set_vms" $disk_names | sed 's/^/  disk /'
})
if [ -n "$left" ]; then
  echo "teardown.sh: set $SET_NAME is not gone; still there (name,zone[,used by]):" >&2
  printf '%s\n' "$left" >&2
  exit 1
fi
if [ "$DRY_RUN" = 1 ]; then
  echo "set $SET_NAME: dry run, nothing deleted"
else
  echo "set $SET_NAME: no VM or disk of it is left"
fi
# Every benchmark VM still there, of every set.
gc compute instances list --filter="labels.purpose=pg-delta-benchmark" \
  --format="table(name,labels.pgd-set:label=SET,zone.basename(),machineType.basename(),status)"
