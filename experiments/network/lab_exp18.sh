#!/bin/bash
# EXP-18, run on dest (docs/experiments/EXP-18-calibrated-contention.md): for each contention level, start K busy
# processes on dest, calibrate dest's hash rate under them, run the sweep, stop them, calibrate again.
#     setsid nohup bash experiments/network/lab_exp18.sh SOURCE_INTERNAL_IP > /data/exp04-results/exp18-lab.log 2>&1 &
set -u
SRC=$1
cd ~/pgd
R=/data/exp04-results
COMMON=(--patterns hot --repeats 3 --manifest-checksums CRC32C --methods full,pgdelta_direct_inplace)

level() {  # NAME K ARGS...
  local name=$1 k=$2 burn=''
  shift 2
  local out=$R/$name
  mkdir -p "$out"
  if [ "$k" -gt 0 ]; then
    python3 experiments/network/agent.py burn --procs "$k" --seconds 7200 > "$out/burn.json" &
    burn=$!
    sleep 2
  fi
  python3 experiments/network/calibrate.py > "$out/calibrate-dest.json"
  python3 experiments/network/run_exp04.py --source-ip "$SRC" --out "$out" "${COMMON[@]}" "$@" > "$out/driver.log" 2>&1
  echo "$(date -u +%T) $name: run_exp04 exit $?"
  if [ -n "$burn" ]; then
    pkill -P "$burn" 2>/dev/null
    kill "$burn" 2>/dev/null
    wait "$burn" 2>/dev/null
  fi
  python3 experiments/network/calibrate.py > "$out/calibrate-dest-after.json"
}

level exp18-k0 0 --speeds 2,unthrottled --order-seed 18
level exp18-k2 2 --speeds 2,unthrottled --order-seed 19
level exp18-k4 4 --speeds 2,unthrottled --order-seed 20
level exp18-k0b 0 --speeds unthrottled --order-seed 21
echo "$(date -u +%T) done"
