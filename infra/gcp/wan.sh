#!/bin/bash
# EXP-13 (T12): the emulated WAN on this VM's interface, toward the other VM only. throttle.sh is unchanged.
#   sudo ./wan.sh 2000mbit 30 10.128.0.12 2222        # rate, one-way delay in ms (r/2), the peer, the control port
#   sudo ./wan.sh 2000mbit 30 10.128.0.12 2222 ens4   # on a named interface
#   sudo ./wan.sh off [DEV]                           # remove it (back to the default qdisc)
#   ./wan.sh show [--json] [DEV]                      # the qdiscs with their counters (--json: tc -s -j)
#   DRY_RUN=1 ./wan.sh 2000mbit 30 10.128.0.12 2222 ens4   # print the tc commands instead of running them
# The tree, on both VMs, each with half the round trip:
#   1:    prio with two bands; its priomap sends everything to band 0, so what no filter picks is plain.
#   1:1   the plain band: neither delayed nor shaped. The harness's control path (TCP port CONTROL_PORT, as
#         source or destination port), the Mac's SSH and the metadata server go here.
#   1:2   the delayed band: every other packet to PEER_IP (u32 filters, the exceptions first):
#         20: netem delay DELAY_ONE_WAY_MS limit 1000000, then its child
#         30: tbf at RATE, with throttle.sh's burst (10 ms of tokens, at least 128 KiB) and a queue of
#             max(50 ms, the round trip): one CUBIC flow keeps the link full after a loss only if the queue
#             holds about 0.43 of the bandwidth-delay product.
# netem before tbf: packets wait in netem's delay line, then queue at the shaper, so the delay line stays
# apart from the bottleneck queue. With DELAY_ONE_WAY_MS 0 (the anchor) the tree is the same, delay 0.
# RATE is an integer with a tc unit (bit, kbit, mbit or gbit); BURST=bytes overrides the burst, and QUEUE_MS=ms the
# bottleneck queue (EXP-26 sets it in round trips: its depth decides how BBR shares the link with CUBIC).
set -euo pipefail
action=${1:?usage: wan.sh RATE DELAY_ONE_WAY_MS PEER_IP CONTROL_PORT [DEV] | off [DEV] | show [--json] [DEV]}
NETEM_LIMIT=1000000
QUEUE_FLOOR_MS=50

default_dev() {
  ip route show default 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}'
}

# Each tc command, or with DRY_RUN=1 its words on one line (what the orchestrator records and the tests read).
tc_run() {
  if [ "${DRY_RUN:-0}" = 1 ]; then
    echo "tc $*"
  else
    tc "$@"
  fi
}

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

case "$action" in
  off)
    dev=${2:-$(default_dev)}
    [ -n "$dev" ] || { echo "cannot find the default interface" >&2; exit 2; }
    if [ "${DRY_RUN:-0}" = 1 ]; then
      tc_run qdisc del dev "$dev" root
      exit 0
    fi
    tc qdisc del dev "$dev" root 2>/dev/null || true
    tc -s qdisc show dev "$dev"
    ;;
  show)
    json=0
    if [ "${2:-}" = --json ]; then json=1; shift; fi
    dev=${2:-$(default_dev)}
    [ -n "$dev" ] || { echo "cannot find the default interface" >&2; exit 2; }
    if [ "$json" = 1 ]; then
      tc -s -j qdisc show dev "$dev"
    else
      tc -s qdisc show dev "$dev"
      tc filter show dev "$dev"
    fi
    ;;
  *)
    rate=$action
    delay=${2:?usage: wan.sh RATE DELAY_ONE_WAY_MS PEER_IP CONTROL_PORT [DEV]}
    peer=${3:?usage: wan.sh RATE DELAY_ONE_WAY_MS PEER_IP CONTROL_PORT [DEV]}
    port=${4:?usage: wan.sh RATE DELAY_ONE_WAY_MS PEER_IP CONTROL_PORT [DEV]}
    dev=${5:-$(default_dev)}
    [ -n "$dev" ] || { echo "cannot find the default interface" >&2; exit 2; }
    [[ "$delay" =~ ^[0-9]+(\.[0-9]+)?$ ]] || { echo "bad one-way delay (ms): $delay" >&2; exit 2; }
    [[ "$peer" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { echo "bad peer address: $peer" >&2; exit 2; }
    [[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -ge 1 ] && [ "$port" -le 65535 ] \
      || { echo "bad control port: $port" >&2; exit 2; }
    bits=$(rate_bits "$rate")
    burst=${BURST:-$((bits / 8 / 100))}
    [ "$burst" -ge 131072 ] || burst=131072
    # The bottleneck queue: max(50 ms, the round trip, twice the one-way delay).
    queue=$(awk -v d="$delay" -v floor="$QUEUE_FLOOR_MS" 'BEGIN { q = 2 * d; if (q < floor) q = floor; printf "%g", q }')
    if [ -n "${QUEUE_MS:-}" ]; then
      [[ "$QUEUE_MS" =~ ^[0-9]+(\.[0-9]+)?$ ]] || { echo "bad QUEUE_MS: $QUEUE_MS" >&2; exit 2; }
      queue=$QUEUE_MS
    fi
    if [ "${DRY_RUN:-0}" = 1 ]; then
      tc_run qdisc del dev "$dev" root
    else
      tc qdisc del dev "$dev" root 2>/dev/null || true
    fi
    tc_run qdisc add dev "$dev" root handle 1: prio bands 2 priomap 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0
    tc_run qdisc add dev "$dev" parent 1:2 handle 20: netem delay "${delay}ms" limit "$NETEM_LIMIT"
    tc_run qdisc add dev "$dev" parent 20:1 handle 30: tbf rate "$rate" burst "$burst" latency "${queue}ms"
    # The control path stays plain, whichever end of it this VM is (pref 1, before the delayed band's filter).
    tc_run filter add dev "$dev" parent 1: protocol ip prio 1 u32 match ip dst "$peer/32" \
      match ip protocol 6 0xff match ip sport "$port" 0xffff flowid 1:1
    tc_run filter add dev "$dev" parent 1: protocol ip prio 1 u32 match ip dst "$peer/32" \
      match ip protocol 6 0xff match ip dport "$port" 0xffff flowid 1:1
    tc_run filter add dev "$dev" parent 1: protocol ip prio 2 u32 match ip dst "$peer/32" flowid 1:2
    if [ "${DRY_RUN:-0}" != 1 ]; then
      tc -s qdisc show dev "$dev"
      tc filter show dev "$dev"
    fi
    ;;
esac
