#!/bin/bash
# EXP-64, run on dest of the ha set (docs/experiments/EXP-64-fast-path-beyond-memory.md): EXP-48's 18 GB database
# on 8 GB VMs with balanced persistent disks, its source live with summarize_wal on. Per pattern (hot, then uniform,
# 1% each), B0 is a base backup taken before the change; then each run starts from a fresh copy of B0 with both
# VMs' caches dropped: pgdelta in place hashing both copies ("hash"), the same with --fast-path summaries ("fast"),
# and one pg_basebackup stream into a new directory ("full"). Each fast copy is started and its pgbench_accounts
# compared with the source's (rows and the sum of abalance).
#     setsid nohup bash experiments/network/lab_exp64.sh SOURCE_IP [REPEATS] [SEED] \
#         > /data/exp04-results/exp64-lab.log 2>&1 < /dev/null &
# SCALE (default 600, about 18 GB) and OUT change the size and the results directory (the smoke: SCALE=20).
# PATTERNS (default "hot uniform") and ARMS (default "hash fast full") pick the cells; an arm "fastN" is the fast
# path with --read-ahead N (EXP-64b: PATTERNS=uniform ARMS="hash fast fast1 fast8 full").
set -u
SRC=$1
REPEATS=${2:-2}
SEED=${3:-640}
SCALE=${SCALE:-600}
PATTERNS=${PATTERNS:-hot uniform}
ARMS=${ARMS:-hash fast full}
DEST=$(hostname -I | awk '{print $1}')
cd ~/pgd
R=${OUT:-/data/exp04-results/exp64}
W=/data/exp64
rm -rf "$R" "$W"; mkdir -p "$R" "$W"
KEY=$HOME/.ssh/exp04_ed25519
# -n: inside the plan loop ssh must not read the plan from stdin. The source on the control port (outside wan.sh).
SSRC=(ssh -n -i "$KEY" -p 2222 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$SRC")
export PATH="$PATH:/usr/sbin:/sbin"
PGBIN=/usr/lib/postgresql/17/bin
ROOT=/data/exp64/src
B0=$W/B0 B=$W/B FULL=$W/full
CONN="host=$ROOT/s port=5432 user=study dbname=postgres"
on_src() { "${SSRC[@]}" "export PATH=\$PATH:/usr/sbin:/sbin; cd ~/pgd; $1"; }
psrc() { on_src "$PGBIN/psql -h $ROOT/s -p 5432 -U study -d postgres -qAtX -v ON_ERROR_STOP=1 -c \"$1\""; }
drop() { sync; echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null; on_src "sync; echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null"; }
STATE="SELECT count(*) || ':' || sum(abalance) FROM pgbench_accounts"
WORKLOAD_ARGS="--root $ROOT --bin-dir $PGBIN --log-dir $W/logs --scale $SCALE --fraction 0.01 --seed 20261022 --hot-percent 1.0 --batch-size 100"

echo "$(date -u +%T) set-up: scale $SCALE"
# setup stops a cluster left at ROOT (the smoke's) before it clears ROOT.
on_src "mkdir -p $W && python3 experiments/network/source_cluster.py setup $WORKLOAD_ARGS --pattern hot \
  --listen-ip $SRC --client-ip $DEST > $W/setup.json" || { echo "set-up failed"; exit 1; }
# Summaries from here on: on before any old copy is taken, so they cover each B0's start onwards.
on_src "echo 'summarize_wal = on  # EXP-64' >> $ROOT/data/postgresql.conf && $PGBIN/pg_ctl -D $ROOT/data reload > /dev/null"
psrc "CHECKPOINT"
for x in $(seq 120); do
  [ "$(psrc "SELECT summarized_lsn > '0/0' FROM pg_get_wal_summarizer_state()")" = t ] && break; sleep 1
done
VERSION=$(psrc "SHOW server_version_num")
SUMMARIZED=$(psrc "SELECT summarized_lsn FROM pg_get_wal_summarizer_state()")
SIZE=$(psrc "SELECT pg_database_size('postgres')")
echo "{\"server_version_num\": $VERSION, \"summarized_lsn\": \"$SUMMARIZED\", \"database_bytes\": $SIZE, \"scale\": $SCALE}" \
  > "$R/source.json"
echo "$(date -u +%T) server $VERSION, database $((SIZE / 1000000)) MB, summarizer at $SUMMARIZED"
[ "$VERSION" -ge 170011 ] || { echo "the server is $VERSION: the fast path needs 17.11 or later; failed"; exit 1; }
# The session's own disk test on dest (EXP-48's model reads its rates): 2 GiB written, then read, direct I/O.
dd if=/dev/zero of="$W/ddtest" bs=1M count=2048 oflag=direct 2> "$R/dd-write.txt"
dd if="$W/ddtest" of=/dev/null bs=1M iflag=direct 2> "$R/dd-read.txt"
rm -f "$W/ddtest"

i=0
for pattern in $PATTERNS; do
  echo "$(date -u +%T) $pattern: B0, a base backup before the change"
  rm -rf "$B0"
  $PGBIN/pg_basebackup -h "$SRC" -p 5432 -U study -D "$B0" -X stream -c fast > "$R/B0-$pattern.log" 2>&1 \
    || { echo "B0 failed"; exit 1; }
  on_src "python3 experiments/network/source_cluster.py workload $WORKLOAD_ARGS --pattern $pattern \
    > $W/workload-$pattern.json" || { echo "workload failed"; exit 1; }
  on_src "cat $W/workload-$pattern.json" > "$R/workload-$pattern.json"
  SOURCE_STATE=$(psrc "$STATE")
  echo "$(date -u +%T) $pattern: workload done; source state $SOURCE_STATE"
  python3 - "$REPEATS" "$SEED" "$pattern" $ARMS > "$R/plan-$pattern.txt" <<'EOF'
import random, sys
repeats, seed, pattern, arms = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4:]
runs = [(arm, k) for arm in arms for k in range(repeats)]
random.Random(seed + (pattern == 'uniform')).shuffle(runs)
for arm, k in runs:
    print(arm, k)
EOF
  while read -r arm k; do
    d="$R/run-$(printf %02d "$i")"
    mkdir -p "$d"
    rm -rf "$B" "$FULL"
    copied=null
    if [ "$arm" != full ]; then
      c0=$(date +%s.%N); cp -a "$B0" "$B"; c1=$(date +%s.%N)
      copied=$(python3 -c "print(round($c1 - $c0, 2))")
    fi
    drop
    rstatus=null status=
    if [ "$arm" = full ]; then
      t0=$(date +%s.%N)
      $PGBIN/pg_basebackup -h "$SRC" -p 5432 -U study -D "$FULL" -X stream -c fast > "$d/full.log" 2>&1
      status=$?
      t1=$(date +%s.%N)
    else
      case "$arm" in
        fast) fast='--fast-path summaries' ;;
        fast*) fast="--fast-path summaries --read-ahead ${arm#fast}" ;;
        *) fast= ;;
      esac
      python3 -m pgdelta receive --basis "$B" --in-place --listen "$DEST:7000" \
        --basis-pg-controldata "$PGBIN/pg_controldata" --jobs 4 > "$d/receive.log" 2>&1 &
      rp=$!
      for x in $(seq 200); do ss -ltn | grep -q ':7000 ' && break; sleep 0.05; done
      t0=$(date +%s.%N)
      on_src "python3 -m pgdelta send --source $ROOT/data --source-kind live --pg-conn '$CONN' --connect $DEST:7000 \
        --pg-controldata $PGBIN/pg_controldata --jobs 4 --hash blake3 $fast --stats-json /tmp/exp64-send.json" \
        > "$d/send.log" 2>&1
      status=$?
      wait "$rp"; rstatus=$?
      t1=$(date +%s.%N)
      on_src "cat /tmp/exp64-send.json" > "$d/send.json"
    fi
    verify=null
    if [ "${arm#fast}" != "$arm" ] && [ "$status" = 0 ]; then
      # The refreshed copy recovers from its backup_label; its pgbench_accounts must equal the source's.
      $PGBIN/pg_ctl -D "$B" -l "$d/copy.log" -w -t 1800 \
        -o "-p 5433 -k /tmp -c listen_addresses='' -c summarize_wal=off" start > /dev/null 2>&1
      COPY_STATE=$($PGBIN/psql -h /tmp -p 5433 -U study -d postgres -qAtX -c "$STATE" 2>&1)
      $PGBIN/pg_ctl -D "$B" -m fast -w stop > /dev/null 2>&1
      verify="{\"source\": \"$SOURCE_STATE\", \"copy\": \"$COPY_STATE\", \"match\": $([ "$SOURCE_STATE" = "$COPY_STATE" ] && echo true || echo false)}"
    fi
    printf '{"run": %s, "pattern": "%s", "arm": "%s", "repeat": %s, "t0": %s, "t1": %s, "status": %s, "receiver_status": %s, "copy_seconds": %s, "verify": %s}\n' \
      "$i" "$pattern" "$arm" "$k" "$t0" "$t1" "$status" "$rstatus" "$copied" "$verify" > "$d/meta.json"
    echo "$(date -u +%T) run $i: $pattern $arm repeat $k: $(python3 -c "print(round($t1 - $t0, 2))") s, exit $status" \
      "$([ "$verify" != null ] && python3 -c "import json,sys; print('match', json.loads(sys.argv[1])['match'])" "$verify")"
    i=$((i + 1))
  done < "$R/plan-$pattern.txt"
done
rm -rf "$B" "$FULL" "$B0"
echo "$(date -u +%T) done"
