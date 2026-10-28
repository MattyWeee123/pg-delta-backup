#!/bin/bash
# EXP-76: the /proc/diskstats line of the device behind /data, with the wall-clock time before it, every INTERVAL
# seconds (default 0.25) until killed. lab_exp64.sh runs one on each VM around every run when SERIES=1.
#     bash experiments/network/disk_series.sh [INTERVAL] > series.txt &
dev=$(basename "$(findmnt -no SOURCE /data)")
while :; do
  printf '%s ' "$(date +%s.%N)"
  awk -v d="$dev" '$3 == d' /proc/diskstats
  sleep "${1:-0.25}"
done
