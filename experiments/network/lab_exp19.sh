#!/bin/bash
# EXP-19, run on dest (docs/experiments/EXP-19-two-part-model.md): levels 0, 3 busy processes, disk contention, 0 again.
#     setsid nohup bash experiments/network/lab_exp19.sh SOURCE_INTERNAL_IP > /data/exp04-results/exp19-lab.log 2>&1 &
set -u
SRC=$1
cd ~/pgd
R=/data/exp04-results
CAL=$R/exp19-calibration
mkdir -p "$CAL"
COMMON=(--patterns hot --repeats 3 --manifest-checksums CRC32C --methods full,pgdelta_direct_inplace --speeds 2,unthrottled)

level() {  # NAME KIND ARGS... ; KIND is 0, a number of busy processes, or io
  local name=$1 kind=$2 bg=''
  shift 2
  if [ "$kind" = io ]; then
    bash -c 'while true; do dd if=/dev/zero of=/data/exp19-ioload bs=1M count=4096 oflag=direct status=none; done' &
    bg=$!
    sleep 3
  elif [ "$kind" -gt 0 ]; then
    python3 experiments/network/agent.py burn --procs "$kind" --seconds 7200 > "$CAL/$name-burn.json" &
    bg=$!
    sleep 2
  fi
  python3 experiments/network/calibrate.py --disk /data > "$CAL/$name-before.json"
  python3 experiments/network/run_exp04.py --source-ip "$SRC" --out "$R/$name" "${COMMON[@]}" "$@" > "$CAL/$name-driver.log" 2>&1
  local status=$?
  echo "$(date -u +%T) $name: run_exp04 exit $status"
  if [ -n "$bg" ]; then
    pkill -P "$bg" 2>/dev/null
    kill "$bg" 2>/dev/null
    wait "$bg" 2>/dev/null
    rm -f /data/exp19-ioload
  fi
  python3 experiments/network/calibrate.py --disk /data > "$CAL/$name-after.json"
}

level exp19-k0 0 --order-seed 190
level exp19-k3 3 --order-seed 191
level exp19-io io --order-seed 192
level exp19-k0b 0 --order-seed 193
echo "$(date -u +%T) done"
