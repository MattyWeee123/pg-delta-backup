#!/bin/bash
# EXP-26, run on dest (docs/experiments/EXP-26-bbr-fairness.md): one newcomer flow, cubic or BBR, joins cubic
# competitors on the emulated 2 Gbit/s bottleneck; the competitors' rate while it runs is the harm it does.
#     setsid nohup bash experiments/network/lab_exp26.sh SOURCE_INTERNAL_IP [REPEATS] [SEED] [PLAN] \
#         > /data/exp04-results/exp26-lab.log 2>&1 &
# PLAN: full (the registered grid, the default) or smoke (one cell, both newcomers, once each); CELLS="rtt,q,n;..."
# runs only those cells (the lean revision's extra repeats).
# Every flow goes source -> dest, as pgdelta's data does: the competitors (iperf3 -P N -C cubic) for COMP_SECONDS
# from t = 0, the newcomer (iperf3 -C cubic or bbr) from t = JOIN to the end; the window is t = PING_FROM to the end.
# Lean revision (October 25, before the grid ran): 50 s runs, the newcomer at 10 s, the window 20-50 s (registered:
# 100, 20 and 40-100). Both VMs carry
# wan.sh's tree with half the round trip each and tuned TCP buffers (maxima 128 MiB), so one flow can fill the link
# at 120 ms. The source allows BBR for unprivileged sockets, as EXP-24 did; its default stays cubic.
set -u
SRC=$1
REPEATS=${2:-1}
SEED=${3:-260}
PLAN=${4:-full}
DEST=$(hostname -I | awk '{print $1}')
cd ~/pgd
R=/data/exp04-results/exp26${PLAN/full/}
mkdir -p "$R"
# -n: inside the plan loop ssh must not read the plan from stdin (smoke fix).
SSH=(ssh -n -i "$HOME/.ssh/exp04_ed25519" -p 2222 -o BatchMode=yes -o StrictHostKeyChecking=accept-new)
export PATH="$PATH:/usr/sbin:/sbin"
RATE=2000mbit
BURST_MS=10  # wan.sh's burst at 2 Gbit/s (10 ms of tokens); tc's tbf queue is rate x latency + burst
COMP_SECONDS=${COMP_SECONDS:-50} JOIN=${JOIN:-10} PING_FROM=${PING_FROM:-20}
NEW_SECONDS=$((COMP_SECONDS - JOIN)) PING_SECONDS=$((COMP_SECONDS - PING_FROM))

on_source() { "${SSH[@]}" "$SRC" "export PATH=\$PATH:/usr/sbin:/sbin; $1"; }
both() { sudo bash -c "$1"; on_source "sudo bash -c '$1'"; }

TUNED='net.ipv4.tcp_rmem="4096 131072 134217728" net.ipv4.tcp_wmem="4096 16384 134217728"'
both "modprobe tcp_bbr; sysctl -qw $TUNED"
on_source "sudo sysctl -qw net.ipv4.tcp_allowed_congestion_control='reno cubic bbr'"
{
  echo "dest: $(sysctl -n net.ipv4.tcp_congestion_control) | $(sysctl -n net.ipv4.tcp_rmem) | $(sysctl -n net.ipv4.tcp_wmem)"
  echo "source: $(on_source 'sysctl -n net.ipv4.tcp_congestion_control net.ipv4.tcp_allowed_congestion_control net.ipv4.tcp_rmem net.ipv4.tcp_wmem' | tr '\n' '|')"
  echo "kernel: $(uname -r); $(iperf3 --version | head -1)"
} | tee "$R/hosts.txt"

python3 - "$REPEATS" "$SEED" "$PLAN" "${CELLS:-}" > "$R/plan.txt" <<'EOF'
import random, sys
repeats, seed, plan, only = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
# (round trip ms, queue depth in round trips, cubic competitors)
cells = ([(60, q, n) for q in (0.5, 1, 2, 4) for n in (1, 4)] + [(20, 1, 1), (120, 1, 1)]
         if plan == 'full' else [(60, 1, 1)])
if only:
    cells = [(int(r), float(q) if '.' in q else int(q), int(n)) for r, q, n in (c.split(',') for c in only.split(';'))]
runs = [(rtt, q, n, cc, k) for rtt, q, n in cells for cc in ('cubic', 'bbr') for k in range(repeats)]
random.Random(seed).shuffle(runs)
for i, run in enumerate(runs):
    print(i, *run)
EOF
echo "$(date -u +%T) plan: $(wc -l < "$R/plan.txt") runs"

while read -r i rtt q n cc k; do
  half=$(awk -v r="$rtt" 'BEGIN { printf "%g", r / 2 }')
  queue=$(awk -v r="$rtt" -v q="$q" -v b="$BURST_MS" 'BEGIN { printf "%g", q * r - b }')
  d="$R/run-$(printf %03d "$i")"
  mkdir -p "$d"
  printf '{"run": %s, "rtt_ms": %s, "depth_rtt": %s, "competitors": %s, "newcomer": "%s", "repeat": %s, "queue_ms": %s, "burst_ms": %s, "comp_window": [%s, %s], "new_window": [%s, %s], "seed": %s, "utc": "%s"}\n' \
    "$i" "$rtt" "$q" "$n" "$cc" "$k" "$queue" "$BURST_MS" "$PING_FROM" "$COMP_SECONDS" "$((PING_FROM - JOIN))" "$NEW_SECONDS" "$SEED" "$(date -u +%FT%TZ)" > "$d/meta.json"
  sudo QUEUE_MS="$queue" bash infra/gcp/wan.sh "$RATE" "$half" "$SRC" 2222 > /dev/null
  on_source "sudo QUEUE_MS=$queue bash ~/pgd/infra/gcp/wan.sh $RATE $half $DEST 2222 > /dev/null"
  both "ip tcp_metrics flush all"
  pkill -x iperf3; sleep 0.5
  iperf3 -s -p 5201 -D -J; iperf3 -s -p 5202 -D -J; sleep 0.5  # -J: the server's intervals as JSON (smoke fix)
  on_source "bash ~/pgd/infra/gcp/wan.sh show --json" > "$d/qdisc-before.json"
  on_source "rm -rf /tmp/exp26 && mkdir /tmp/exp26 && cd /tmp/exp26 &&
    (iperf3 -c $DEST -p 5201 -P $n -C cubic -t $COMP_SECONDS -i 1 -J --get-server-output > comp.json 2> comp.err &) &&
    sleep $JOIN &&
    (iperf3 -c $DEST -p 5202 -C $cc -t $NEW_SECONDS -i 1 -J --get-server-output > new.json 2> new.err &) &&
    sleep $((PING_FROM - JOIN)) && ping -D -i 0.2 -w $PING_SECONDS $DEST > ping.txt 2>&1;
    while pgrep -x iperf3 > /dev/null; do sleep 1; done"
  for f in comp.json new.json comp.err new.err ping.txt; do on_source "cat /tmp/exp26/$f" > "$d/$f"; done
  on_source "bash ~/pgd/infra/gcp/wan.sh show --json" > "$d/qdisc-after.json"
  echo "$(date -u +%T) run $i: ${rtt} ms, depth ${q}, ${n} competitor(s), newcomer $cc, repeat $k: comp $(python3 -c "
import json,sys
for f in ('comp','new'):
    j=json.load(open('$d/'+f+'.json')); s=j.get('server_output_json',{}).get('intervals',[])
    lo,hi=(($PING_FROM,$COMP_SECONDS) if f=='comp' else ($((PING_FROM - JOIN)),$NEW_SECONDS))
    w=[x['sum']['bits_per_second'] for x in s if lo<=x['sum']['start']<hi]
    print(f, round(sum(w)/len(w)/1e6) if w else 'none', j.get('end',{}).get('sender_tcp_congestion'), end='; ')
" 2>&1)"
done < "$R/plan.txt"

pkill -x iperf3
sudo bash infra/gcp/wan.sh off > /dev/null; on_source "sudo bash ~/pgd/infra/gcp/wan.sh off > /dev/null"
echo "$(date -u +%T) done"
