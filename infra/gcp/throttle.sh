#!/bin/bash
# Limit this VM's outgoing bandwidth to simulate a slower link.
#   sudo ./throttle.sh 1gbit         # cap egress at 1 Gbit/s
#   sudo ./throttle.sh 500mbit ens4  # on a named interface
#   sudo ./throttle.sh off           # remove the cap (back to the default qdisc)
#   ./throttle.sh show               # print the qdisc and its counters
# A token-bucket filter (tbf) as the root qdisc caps all egress of the
# interface. EXP-04 applies the same rate on both VMs, so both directions are
# shaped. RATE is an integer with a tc unit: bit, kbit, mbit or gbit.
#
# Burst: 10 ms worth of tokens, at least 128 KiB. tbf needs at least
# rate/HZ bytes (5 MB at 10 Gbit/s with HZ=250) and more than one 64 KiB
# GSO packet, otherwise it cannot reach the rate; the old fixed 1 MB burst
# capped fast rates. Override with BURST=bytes. LATENCY (default 50ms) bounds
# the queue.
set -euo pipefail
rate=${1:?usage: throttle.sh RATE|off|show [DEV]}
dev=${2:-$(ip route show default | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}')}
[ -n "$dev" ] || { echo "cannot find the default interface" >&2; exit 2; }

rate_bits() {
  local value=$1 number unit
  number=${value%%[!0-9]*}
  unit=${value#"$number"}
  [ -n "$number" ] || { echo "bad rate: $value" >&2; return 2; }
  case "$unit" in
    bit|'') echo "$number" ;;
    kbit) echo $((number * 1000)) ;;
    mbit) echo $((number * 1000000)) ;;
    gbit) echo $((number * 1000000000)) ;;
    *) echo "bad rate unit in $value (use bit, kbit, mbit or gbit)" >&2; return 2 ;;
  esac
}

case "$rate" in
  show) ;;
  off) tc qdisc del dev "$dev" root 2>/dev/null || true ;;
  *)
    bits=$(rate_bits "$rate")
    burst=${BURST:-$((bits / 8 / 100))}
    [ "$burst" -ge 131072 ] || burst=131072
    tc qdisc replace dev "$dev" root tbf rate "$rate" burst "$burst" latency "${LATENCY:-50ms}"
    ;;
esac
tc -s qdisc show dev "$dev"
