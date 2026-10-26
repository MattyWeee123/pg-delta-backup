#!/bin/bash
# EXP-57 (C-ckpt, lean), run on one VM of the ha set (docs/experiments/EXP-57-checkpoint-burst.md): a busy primary
# with a large dirty set on a balanced persistent disk; pg_backup_start with a fast checkpoint, a spread one, or none.
#     setsid nohup bash experiments/network/lab_exp57.sh [REPEATS] [SEED] > /data/exp04-results/exp57-lab.log 2>&1 &
# SCALE, DIRTY and LOAD_SECONDS in the environment change the database size, the dirtying period and the load's
# length (the smoke: SCALE=20 DIRTY=20 LOAD_SECONDS=120).
set -u
REPEATS=${1:-2}
SEED=${2:-570}
PGBIN=/usr/lib/postgresql/17/bin
SCALE=${SCALE:-400} DIRTY=${DIRTY:-180} LOAD_SECONDS=${LOAD_SECONDS:-500} CLIENTS=16 THREADS=4 SHARE=0.6
D=/data/exp57
R=${OUT:-/data/exp04-results/exp57}
export PATH="$PATH:/usr/sbin:/sbin"
cd ~/pgd
rm -rf "$D" "$R"; mkdir -p "$D" "$R"
q() { "$PGBIN/psql" -h "$D" -U postgres -d bench -qAtX -v ON_ERROR_STOP=1 "$@"; }
"$PGBIN/initdb" -D "$D/data" -U postgres -A trust > "$R/initdb.log" 2>&1 || { echo "initdb failed"; exit 1; }
cat >> "$D/data/postgresql.conf" <<EOF
# EXP-57
shared_buffers = 8GB
max_wal_size = 32GB
checkpoint_timeout = 5min
checkpoint_completion_target = 0.9
log_checkpoints = on
listen_addresses = ''
unix_socket_directories = '$D'
max_connections = 60
EOF
"$PGBIN/pg_ctl" -D "$D/data" -l "$R/postgres.log" -w start > /dev/null || { echo "start failed"; exit 1; }
"$PGBIN/psql" -h "$D" -U postgres -d postgres -qAtX -c 'CREATE DATABASE bench'
echo "$(date -u +%T) init scale $SCALE"
"$PGBIN/pgbench" -h "$D" -U postgres -i -s "$SCALE" -q bench > "$R/pgbench-init.log" 2>&1 || { echo "init failed"; exit 1; }
q -c 'CREATE EXTENSION pg_buffercache' -c 'CREATE EXTENSION pg_prewarm'
q -c "SELECT sum(pg_prewarm(c.oid)) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public' AND c.relkind IN ('r', 'i')" > "$R/prewarm.txt"
q -c 'CHECKPOINT'
echo "$(date -u +%T) database $(q -c "SELECT pg_size_pretty(pg_database_size('bench'))"), prewarmed $(cat "$R/prewarm.txt") blocks"
LOAD=("$PGBIN/pgbench" -h "$D" -U postgres -n -M prepared -c "$CLIENTS" -j "$THREADS" bench)
"${LOAD[@]}" -T 30 > /dev/null 2>&1
caps=$(for k in 1 2 3; do "${LOAD[@]}" -T 30 2>/dev/null | awk '/^tps = / {print $3}'; done | tr '\n' ' ')
C=$(python3 -c "import statistics, sys; print(statistics.median(float(x) for x in sys.argv[1:]))" $caps)
RATE=$(python3 -c "print(int($C * $SHARE // 10 * 10))")
echo "{\"capacity_runs\": \"$caps\", \"C\": $C, \"R\": $RATE, \"share\": $SHARE, \"scale\": $SCALE, \"dirty_seconds\": $DIRTY}" > "$R/rates.json"
echo "$(date -u +%T) C = $C (runs $caps), R = $RATE"
python3 - "$REPEATS" "$SEED" > "$R/plan.txt" <<'EOF'
import random, sys
repeats, seed = int(sys.argv[1]), int(sys.argv[2])
runs = [(arm, k) for arm in ('none', 'fast', 'spread') for k in range(repeats)]
random.Random(seed).shuffle(runs)
for i, (arm, k) in enumerate(runs):
    print(i, arm, k)
EOF
now() { date +%s.%N; }
while read -r i arm k; do
  d="$R/run-$(printf %02d "$i")"
  mkdir -p "$d"
  q -c 'CHECKPOINT'
  "${LOAD[@]}" -R "$RATE" -T "$LOAD_SECONDS" -l --log-prefix "$d/load" --random-seed "$((SEED + i))" > "$d/pgbench.log" 2>&1 &
  lp=$!
  sleep "$DIRTY"
  dirty=$(q -c "SELECT count(*) FROM pg_buffercache WHERE isdirty")
  t0=$(now)
  case "$arm" in
    none) sleep 60; ts=$t0; te=$(now) ;;
    fast|spread) fast=$([ "$arm" = fast ] && echo true || echo false)
      out=$(q -c "SELECT extract(epoch FROM clock_timestamp())" -c "SELECT pg_backup_start('exp57', $fast)" \
                -c "SELECT extract(epoch FROM clock_timestamp())" -c "SELECT lsn FROM pg_backup_stop(false)")
      ts=$(echo "$out" | sed -n 1p); te=$(echo "$out" | sed -n 3p) ;;
  esac
  t1=$(now)
  sleep 10
  printf '{"run": %s, "arm": "%s", "repeat": %s, "t0": %s, "start_called": %s, "start_returned": %s, "t1": %s, "dirty_buffers": %s, "rate": %s}\n' \
    "$i" "$arm" "$k" "$t0" "$ts" "$te" "$t1" "$dirty" "$RATE" > "$d/meta.json"
  wait "$lp"
  echo "$(date -u +%T) run $i: $arm, repeat $k: dirty $dirty buffers, start blocked $(python3 -c "print(round($te - $ts, 1))") s"
done < "$R/plan.txt"
cp "$R/postgres.log" "$R/postgres-final.log"
"$PGBIN/pg_ctl" -D "$D/data" -m fast -w stop > /dev/null
echo "$(date -u +%T) done"
