#!/bin/bash
# EXP-23, run on dest (docs/experiments/EXP-21-tcp-diagnosis.md): cubic and BBR on both VMs, one sweep each.
#     setsid nohup bash experiments/network/lab_exp21.sh SOURCE_INTERNAL_IP > /data/exp04-results/exp23-lab.log 2>&1 &
set -u
SRC=$1
cd ~/pgd
R=/data/exp04-results
SSH=(ssh -i "$HOME/.ssh/exp04_ed25519" -o BatchMode=yes -o StrictHostKeyChecking=accept-new)
export PATH="$PATH:/usr/sbin:/sbin"
COMMON=(--patterns uniform --fractions 0.01 --manifest-checksums CRC32C --dirty-noise-table 5:4,30:4,120:4 --repeats 15
        --control-port 2222 --link-test-seconds 6 --link-test-warmup 3 --tuned-tcp-max-bytes 134217728
        --conditions 2:60:tuned,2:20:default --methods pgdelta_direct_inplace)

both() {  # run a command here and on the source
  sudo bash -c "$1"
  "${SSH[@]}" "$SRC" "sudo bash -c '$1'"
}

setting() {  # NAME CC SSAI SEED
  local name=$1 cc=$2 ssai=$3 seed=$4
  both "modprobe tcp_bbr 2>/dev/null; sysctl -qw net.ipv4.tcp_congestion_control=$cc net.ipv4.tcp_slow_start_after_idle=$ssai"
  echo "$(date -u +%T) $name: $(sysctl -n net.ipv4.tcp_congestion_control) ssai=$(sysctl -n net.ipv4.tcp_slow_start_after_idle) here; source: $("${SSH[@]}" "$SRC" '/usr/sbin/sysctl -n net.ipv4.tcp_congestion_control net.ipv4.tcp_slow_start_after_idle' | tr '\n' ' ')"
  python3 experiments/network/run_exp04.py --source-ip "$SRC" --out "$R/$name" "${COMMON[@]}" --order-seed "$seed" \
    > "$R/$name.driver.log" 2>&1
  echo "$(date -u +%T) $name: run_exp04 exit $?"
}

setting exp23-cubic cubic 1 230
setting exp23-bbr bbr 1 231
both "sysctl -qw net.ipv4.tcp_congestion_control=cubic net.ipv4.tcp_slow_start_after_idle=1"
echo "$(date -u +%T) done (restored cubic, slow start after idle on)"
