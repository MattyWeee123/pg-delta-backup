#!/bin/bash
# EXP-28 (T10b, lean), run on dest (docs/experiments/EXP-28-live-slowdown.md): v1 reading a live, busy primary
# (--source-kind live, in place) against one pg_basebackup stream, by O4's two numbers, with the load's client on
# its own VM.
#     setsid nohup bash experiments/network/lab_exp28.sh SOURCE_IP CLIENT_IP [REPEATS] [SEED] [PLAN] \
#         > /data/exp04-results/exp28-lab.log 2>&1 < /dev/null &
# PLAN: full (the registered grid, the default) or smoke (each arm once, unthrottled, on a short load); RUNS="arm:speed
# ..." runs only those cells (the registered extra repeats), with the set-up skipped when SKIP_SETUP=1.
# The load: EXP-12's bank scripts (commit.sql at 99, abort.sql at 1), open loop at 0.6 of the closed-loop capacity C
# measured here, 32 clients on 4 threads, every transaction logged on the client (tmpfs). Each run: a 15 s settle
# (load only), the timed arm from t0 to t1 on dest's clock, a 10 s tail. The analysis (analysis/exp28.py) reads
# O4's two numbers from the client's log over [t0, t1 + 10 s], mapped to the client's clock by chrony's offsets.
set -u
SRC=$1
CLI=$2
REPEATS=${3:-2}
SEED=${4:-280}
PLAN=${5:-full}
DEST=$(hostname -I | awk '{print $1}')
cd ~/pgd
R=/data/exp04-results/exp28${PLAN/full/}
mkdir -p "$R"
KEY=$HOME/.ssh/exp04_ed25519
# -n: inside the plan loop ssh must not read the plan from stdin. The source on the control port (outside wan.sh).
SSRC=(ssh -n -i "$KEY" -p 2222 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$SRC")
SCLI=(ssh -n -i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$CLI")
export PATH="$PATH:/usr/sbin:/sbin"
PGBIN=/usr/lib/postgresql/17/bin
ROOT=/data/exp28/src
B=/data/exp28/B
FULL=/data/exp28/full
# Smoke revision: 128 clients, not EXP-12's 32. Over the network 32 clients were bound by round trips (C = 7,277,
# the source about a quarter busy); C levels off near 21,000 at 128-192, the source about 70% busy.
SETTLE=15 TAIL=10 NONE_SECONDS=8 CLIENTS=128 THREADS=4 SHARE=0.6
LOAD="$PGBIN/pgbench -n -M prepared -c $CLIENTS -j $THREADS -f commit.sql@99 -f abort.sql@1 -h $SRC -p 5432 -U study loaddb"

on_src() { "${SSRC[@]}" "export PATH=\$PATH:/usr/sbin:/sbin; cd ~/pgd; $1"; }
on_cli() { "${SCLI[@]}" "export PATH=\$PATH:/usr/sbin:/sbin; $1"; }
offsets() {  # chrony's offset of each VM from the same reference (the metadata server), as one JSON line
  python3 - "$(chronyc tracking 2>&1)" "$(on_src 'chronyc tracking' 2>&1)" "$(on_cli 'chronyc tracking' 2>&1)" <<'EOF'
import json, re, sys
out = {}
for role, text in zip(('dest', 'source', 'client'), sys.argv[1:]):
    m = re.search(r'System time\s*:\s*([\d.]+) seconds (fast|slow)', text)
    out[role] = None if not m else float(m.group(1)) * (1 if m.group(2) == 'fast' else -1)
print(json.dumps(out))
EOF
}

if [ "${SKIP_SETUP:-0}" != 1 ]; then
  echo "$(date -u +%T) set-up"
  on_src "python3 experiments/network/source_cluster.py setup --root $ROOT --log-dir /data/exp28/logs --listen-ip $SRC \
    --client-ip $DEST --scale 70 --pattern uniform --fraction 0.01 > /data/exp28-setup.json && \
    echo 'max_connections = 300  # EXP-28: 128 load clients over the network' >> $ROOT/data/postgresql.conf && \
    python3 experiments/network/source_cluster.py loaddb-setup --root $ROOT --log-dir /data/exp28/logs \
      --load-dir /dev/shm/exp28-unused --clients $CLIENTS > /data/exp28-loaddb.json && \
    sed -i '1i host loaddb study $CLI/32 trust' $ROOT/data/pg_hba.conf && \
    $PGBIN/pg_ctl -D $ROOT/data reload" || { echo "set-up failed"; exit 1; }
  rm -rf "$B" "$FULL"; mkdir -p /data/exp28
  $PGBIN/pg_basebackup -h "$SRC" -p 5432 -U study -D "$B" -X stream -c fast || { echo "B failed"; exit 1; }
  echo "$(date -u +%T) B: $(du -sh "$B" | cut -f1)"
  # C: the closed-loop capacity, the median of 3 x 30 s after a 30 s warm-up (EXP-12: 60 s each).
  on_cli "cd ~/pgd/experiments/network/load && $LOAD -T 30 > /dev/null 2>&1"
  caps=$(for k in 1 2 3; do on_cli "cd ~/pgd/experiments/network/load && $LOAD -T 30 2>/dev/null" \
    | awk '/^tps = / {print $3}'; done | tr '\n' ' ')
  C=$(python3 -c "import statistics, sys; print(statistics.median(float(x) for x in sys.argv[1:]))" $caps)
  RATE=$(python3 -c "print(int($C * $SHARE // 100 * 100))")
  echo "{\"capacity_runs\": \"$caps\", \"C\": $C, \"R\": $RATE, \"share\": $SHARE}" > "$R/rates.json"
  echo "$(date -u +%T) C = $C (runs $caps), R = $RATE"
else
  RATE=$(python3 -c "import json; print(json.load(open('$R/rates.json'))['R'])")
fi

python3 - "$REPEATS" "$SEED" "$PLAN" "${RUNS:-}" > "$R/plan.txt" <<'EOF'
import random, sys
repeats, seed, plan, only = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
arms = ['none', 'full', 'v1_j1', 'v1_j2', 'v1_j4']
cells = [(a, s) for a in arms for s in ('unthrottled', '2gbit')] if plan == 'full' else [(a, 'unthrottled') for a in arms]
if only:
    cells = [tuple(c.split(':')) for c in only.split()]
runs = [(a, s, k) for a, s in cells for k in range(repeats if plan == 'full' else 1)]
random.Random(seed).shuffle(runs)
for i, run in enumerate(runs):
    print(i, *run)
EOF
LOAD_SECONDS=$(( $(wc -l < "$R/plan.txt") * 60 + 600 ))
echo "$(date -u +%T) plan: $(wc -l < "$R/plan.txt") runs; the load at R = $RATE for up to $LOAD_SECONDS s"
on_cli "rm -rf /dev/shm/exp28 && mkdir -p /dev/shm/exp28 && cd ~/pgd/experiments/network/load && \
  (setsid nohup $LOAD -R $RATE -T $LOAD_SECONDS --random-seed $SEED -l --log-prefix /dev/shm/exp28/load -P 1 \
   --progress-timestamp > /dev/shm/exp28/progress.txt 2>&1 < /dev/null &)"
offsets > "$R/offsets-start.json"
sleep 60  # the load's warm-up

while read -r i arm speed k; do
  d="$R/run-$(printf %03d "$i")"
  mkdir -p "$d"
  if [ "$speed" = 2gbit ]; then
    sudo bash infra/gcp/wan.sh 2000mbit 0 "$SRC" 2222 > /dev/null
    on_src "sudo bash infra/gcp/wan.sh 2000mbit 0 $DEST 2222 > /dev/null"
  else
    sudo bash infra/gcp/wan.sh off > /dev/null
    on_src "sudo bash infra/gcp/wan.sh off > /dev/null"
  fi
  rp=
  if [ "${arm#v1_j}" != "$arm" ]; then  # the receiver listens before the clock starts
    python3 -m pgdelta receive --basis "$B" --in-place --listen 0.0.0.0:7000 \
      --basis-pg-controldata "$PGBIN/pg_controldata" --jobs 4 > "$d/receive.log" 2>&1 &
    rp=$!
    for x in $(seq 100); do ss -ltn | grep -q ':7000 ' && break; sleep 0.05; done
  fi
  sleep "$SETTLE"
  cpu0=$(on_src "head -1 /proc/stat")
  t0=$(date +%s.%N)
  case "$arm" in
    none) sleep "$NONE_SECONDS"; status=0 ;;
    full) $PGBIN/pg_basebackup -h "$SRC" -p 5432 -U study -D "$FULL" -X stream -c fast > "$d/full.log" 2>&1
          status=$? ;;
    v1_j*) on_src "python3 -m pgdelta send --source $ROOT/data --source-kind live \
             --pg-conn 'host=$ROOT/s port=5432 user=study dbname=postgres' --connect $DEST:7000 \
             --pg-controldata $PGBIN/pg_controldata --jobs ${arm#v1_j} --stats-json /tmp/exp28-send.json" \
             > "$d/send.log" 2>&1
           status=$?
           wait "$rp"; rstatus=$? ;;
  esac
  t1=$(date +%s.%N)
  cpu1=$(on_src "head -1 /proc/stat")
  [ -n "$rp" ] && on_src "cat /tmp/exp28-send.json" > "$d/send.json"
  printf '{"run": %s, "arm": "%s", "speed": "%s", "repeat": %s, "t0": %s, "t1": %s, "status": %s, "receiver_status": %s, "cpu0": "%s", "cpu1": "%s", "rate": %s}\n' \
    "$i" "$arm" "$speed" "$k" "$t0" "$t1" "$status" "${rstatus:-null}" "$cpu0" "$cpu1" "$RATE" > "$d/meta.json"
  rstatus=
  sleep "$TAIL"
  rm -rf "$FULL"
  echo "$(date -u +%T) run $i: $arm, $speed, repeat $k: $(python3 -c "print(round($t1 - $t0, 2))") s, exit $status"
done < "$R/plan.txt"

offsets > "$R/offsets-end.json"
on_cli "pkill -ALRM -x pgbench; sleep 5; cat /dev/shm/exp28/load* | gzip -1" > "$R/load-log.gz"
on_cli "cat /dev/shm/exp28/progress.txt" > "$R/progress.txt"
sudo bash infra/gcp/wan.sh off > /dev/null; on_src "sudo bash infra/gcp/wan.sh off > /dev/null"
echo "$(date -u +%T) done"
