#!/bin/bash
# EXP-65, run on dest of the ha set (docs/experiments/EXP-65-archive-catch-up.md): a standby B that fell behind,
# brought current three ways from a fresh copy of it each time, both VMs' caches dropped:
#   replay  the standard: B replays the source's archived WAL through restore_command until it reaches the source's
#           LSN. The archive is copied to dest beforehand, untimed, so replay pays no fetch (its floor);
#   fast    pgdelta live, in place, --fast-path summaries, then the copy started until it accepts connections;
#   full    one pg_basebackup -X stream into a new directory, then started the same way.
# Two lags: "hot10" (the same 1% hot rows updated 10 times, many WAL records over few pages) and "uniform" (1% of
# rows once, spread over about 28% of the pages). Each copy's pgbench_accounts must equal the source's.
#     setsid nohup bash experiments/network/lab_exp65.sh SOURCE_IP [REPEATS] [SEED] \
#         > /data/exp04-results/exp65-lab.log 2>&1 < /dev/null &
# SCALE (default 600, about 18 GB) and OUT change the size and the results directory (the smoke: SCALE=20).
set -u
SRC=$1
REPEATS=${2:-2}
SEED=${3:-650}
SCALE=${SCALE:-600}
DEST=$(hostname -I | awk '{print $1}')
cd ~/pgd
R=${OUT:-/data/exp04-results/exp65}
W=/data/exp65
rm -rf "$R" "$W"; mkdir -p "$R" "$W/archive"
KEY=$HOME/.ssh/exp04_ed25519
SSRC=(ssh -n -i "$KEY" -p 2222 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$SRC")
export PATH="$PATH:/usr/sbin:/sbin"
PGBIN=/usr/lib/postgresql/17/bin
ROOT=$W/src
B0=$W/B0 B=$W/B FULL=$W/full
CONN="host=$ROOT/s port=5432 user=study dbname=postgres"
on_src() { "${SSRC[@]}" "export PATH=\$PATH:/usr/sbin:/sbin; cd ~/pgd; $1"; }
psrc() { on_src "$PGBIN/psql -h $ROOT/s -p 5432 -U study -d postgres -qAtX -v ON_ERROR_STOP=1 -c \"$1\""; }
pcopy() { $PGBIN/psql -h /tmp -p 5433 -U study -d postgres -qAtX -c "$1" 2>/dev/null; }
drop() { sync; echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null; on_src "sync; echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null"; }
start_copy() {  # $1: data directory; started on 5433 without archiving or summaries; returns when it accepts connections
  $PGBIN/pg_ctl -D "$1" -l "$2" -w -t 3600 \
    -o "-p 5433 -k /tmp -c listen_addresses='' -c archive_mode=off -c summarize_wal=off" start > /dev/null 2>&1
}
stop_copy() { $PGBIN/pg_ctl -D "$1" -m fast -w stop > /dev/null 2>&1; }
STATE="SELECT count(*) || ':' || sum(abalance) FROM pgbench_accounts"
WORKLOAD_ARGS="--root $ROOT --bin-dir $PGBIN --log-dir $W/logs --scale $SCALE --fraction 0.01 --seed 20261022 --hot-percent 1.0 --batch-size 100"

echo "$(date -u +%T) set-up: scale $SCALE"
on_src "mkdir -p $W/archive && python3 experiments/network/source_cluster.py setup $WORKLOAD_ARGS --pattern hot \
  --listen-ip $SRC --client-ip $DEST > $W/setup.json" || { echo "set-up failed"; exit 1; }
on_src "python3 experiments/network/source_cluster.py archive --root $ROOT --bin-dir $PGBIN --log-dir $W/logs \
  --command 'cp %p $W/archive/%f' > $W/archive.json" || { echo "archive set-up failed"; exit 1; }
VERSION=$(psrc "SHOW server_version_num")
echo "{\"server_version_num\": $VERSION, \"summarize_wal\": \"$(psrc "SHOW summarize_wal")\", \"database_bytes\": $(psrc "SELECT pg_database_size('postgres')"), \"scale\": $SCALE}" > "$R/source.json"
[ "$VERSION" -ge 170011 ] || { echo "the server is $VERSION: the fast path needs 17.11 or later; failed"; exit 1; }

i=0
for lag in hot10 uniform; do
  echo "$(date -u +%T) $lag: B0, a standby of the source, caught up and stopped cleanly"
  on_src "rm -f $W/archive/*"  # WAL from before this B0 is never needed: what is archived from here is this lag's
  rm -rf "$B0"
  $PGBIN/pg_basebackup -h "$SRC" -p 5432 -U study -D "$B0" -X stream -c fast -R > "$R/B0-$lag.log" 2>&1 \
    || { echo "B0 failed"; exit 1; }
  start_copy "$B0" "$R/B0-$lag-server.log"
  TARGET=$(psrc "SELECT pg_current_wal_lsn()")
  for x in $(seq 600); do [ "$(pcopy "SELECT pg_last_wal_replay_lsn() >= '$TARGET'")" = t ] && break; sleep 1; done
  stop_copy "$B0"
  grep -q 'shut down in recovery' <($PGBIN/pg_controldata "$B0") || { echo "B0 is not a cleanly stopped standby; failed"; exit 1; }
  if [ "$lag" = hot10 ]; then passes=10; pattern=hot; else passes=1; pattern=uniform; fi
  for p in $(seq "$passes"); do
    on_src "python3 experiments/network/source_cluster.py workload $WORKLOAD_ARGS --pattern $pattern \
      > $W/workload-$lag-$p.json" || { echo "workload failed"; exit 1; }
  done
  # The target first, then the switch: the segment holding it is complete and archived before anyone replays.
  S_LSN=$(psrc "SELECT pg_current_wal_lsn()")
  SOURCE_STATE=$(psrc "$STATE")
  LAST=$(psrc "SELECT pg_walfile_name(pg_switch_wal())")
  for x in $(seq 900); do [ "$(psrc "SELECT last_archived_wal >= '$LAST' FROM pg_stat_archiver")" = t ] && break; sleep 1; done
  rm -rf "$W/archive"; mkdir -p "$W/archive"
  on_src "tar -C $W/archive -cf - ." | tar -C "$W/archive" -xf -
  WAL_BYTES=$(du -sb "$W/archive" | cut -f1)
  echo "{\"lag\": \"$lag\", \"source_lsn\": \"$S_LSN\", \"source_state\": \"$SOURCE_STATE\", \"archive_bytes\": $WAL_BYTES, \"archive_files\": $(ls "$W/archive" | wc -l)}" > "$R/lag-$lag.json"
  echo "$(date -u +%T) $lag: source at $S_LSN, state $SOURCE_STATE; archive $((WAL_BYTES / 1000000)) MB copied to dest"
  python3 - "$REPEATS" "$SEED" "$lag" > "$R/plan-$lag.txt" <<'EOF'
import random, sys
repeats, seed, lag = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
runs = [(arm, k) for arm in ('replay', 'fast', 'full') for k in range(repeats)]
random.Random(seed + (lag == 'uniform')).shuffle(runs)
for arm, k in runs:
    print(arm, k)
EOF
  while read -r arm k; do
    d="$R/run-$(printf %02d "$i")"
    mkdir -p "$d"
    rm -rf "$B" "$FULL"
    [ "$arm" != full ] && cp -a "$B0" "$B"
    target=$B
    if [ "$arm" = replay ]; then
      # Catch up from the archive alone: no streaming from the source.
      sed -i '/primary_conninfo/d' "$B/postgresql.auto.conf"
      echo "restore_command = 'cp $W/archive/%f %p'" >> "$B/postgresql.auto.conf"
    fi
    drop
    status=0 rstatus=null
    t0=$(date +%s.%N)
    case "$arm" in
      replay)
        start_copy "$B" "$d/copy.log" || status=$?
        for x in $(seq 7200); do [ "$(pcopy "SELECT pg_last_wal_replay_lsn() >= '$S_LSN'")" = t ] && break; sleep 0.2; done ;;
      fast)
        # EXP-65's settings, pinned since EXP-68 and EXP-70 changed the fast path's defaults.
        python3 -m pgdelta receive --basis "$B" --in-place --listen "$DEST:7000" \
          --basis-pg-controldata "$PGBIN/pg_controldata" --jobs 4 --fsync main > "$d/receive.log" 2>&1 &
        rp=$!
        for x in $(seq 200); do ss -ltn | grep -q ':7000 ' && break; sleep 0.05; done
        on_src "python3 -m pgdelta send --source $ROOT/data --source-kind live --pg-conn '$CONN' --connect $DEST:7000 \
          --pg-controldata $PGBIN/pg_controldata --jobs 4 --hash blake3 --fast-path summaries --read-ahead 0 --fill-gaps 0 \
          --stats-json /tmp/exp65-send.json" > "$d/send.log" 2>&1 || status=$?
        wait "$rp"; rstatus=$?
        [ "$status" = 0 ] && { start_copy "$B" "$d/copy.log" || status=$?; }
        on_src "cat /tmp/exp65-send.json" > "$d/send.json" ;;
      full)
        target=$FULL
        $PGBIN/pg_basebackup -h "$SRC" -p 5432 -U study -D "$FULL" -X stream -c fast > "$d/full.log" 2>&1 || status=$?
        [ "$status" = 0 ] && { start_copy "$FULL" "$d/copy.log" || status=$?; } ;;
    esac
    t1=$(date +%s.%N)
    COPY_STATE=$(pcopy "$STATE")
    stop_copy "$target"
    match=$([ "$COPY_STATE" = "$SOURCE_STATE" ] && echo true || echo false)
    printf '{"run": %s, "lag": "%s", "arm": "%s", "repeat": %s, "t0": %s, "t1": %s, "status": %s, "receiver_status": %s, "source_state": "%s", "copy_state": "%s", "match": %s}\n' \
      "$i" "$lag" "$arm" "$k" "$t0" "$t1" "$status" "$rstatus" "$SOURCE_STATE" "$COPY_STATE" "$match" > "$d/meta.json"
    echo "$(date -u +%T) run $i: $lag $arm repeat $k: $(python3 -c "print(round($t1 - $t0, 2))") s, exit $status, match $match"
    i=$((i + 1))
  done < "$R/plan-$lag.txt"
done
rm -rf "$B" "$FULL" "$B0"
echo "$(date -u +%T) done"
