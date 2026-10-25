#!/bin/bash
# EXP-21, run on dest (docs/experiments/EXP-21-tcp-diagnosis.md): four TCP settings on both VMs, one sweep each.
#     setsid nohup bash experiments/network/lab_exp21.sh SOURCE_INTERNAL_IP > /data/exp04-results/exp21-lab.log 2>&1 &
set -u
SRC=$1
cd ~/pgd
R=/data/exp04-results
COMMON=(--patterns uniform --fractions 0.01 --manifest-checksums CRC32C --dirty-noise-table 5:4,30:4,120:4 --repeats 3
        --control-port 2222 --link-test-seconds 6 --link-test-warmup 3 --tuned-tcp-max-bytes 134217728
        --conditions 2:60:default,2:60:tuned --methods full,pgdelta_direct_inplace)

both() {  # run a command here and on the source
  sudo bash -c "$1"
  ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$SRC" "sudo bash -c '$1'"
}

setting() {  # NAME CC SSAI SEED
  local name=$1 cc=$2 ssai=$3 seed=$4
  both "modprobe tcp_bbr 2>/dev/null; sysctl -qw net.ipv4.tcp_congestion_control=$cc net.ipv4.tcp_slow_start_after_idle=$ssai"
  echo "$(date -u +%T) $name: $(sysctl -n net.ipv4.tcp_congestion_control) ssai=$(sysctl -n net.ipv4.tcp_slow_start_after_idle) here; source: $(ssh -o BatchMode=yes "$SRC" 'sysctl -n net.ipv4.tcp_congestion_control net.ipv4.tcp_slow_start_after_idle' | tr '\n' ' ')"
  python3 experiments/network/run_exp04.py --source-ip "$SRC" --out "$R/$name" "${COMMON[@]}" --order-seed "$seed" \
    > "$R/$name.driver.log" 2>&1
  echo "$(date -u +%T) $name: run_exp04 exit $?"
}

setting exp21-a cubic 1 210
setting exp21-b cubic 0 211
setting exp21-c bbr 1 212
setting exp21-d bbr 0 213
both "sysctl -qw net.ipv4.tcp_congestion_control=cubic net.ipv4.tcp_slow_start_after_idle=1"
echo "$(date -u +%T) done (restored cubic, slow start after idle on)"
