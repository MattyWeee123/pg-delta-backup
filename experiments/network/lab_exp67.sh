#!/bin/bash
# EXP-67, run on dest of the ha set (docs/experiments/EXP-67-rewind-old-primary.md): an old primary after a failover
# (B), brought current from the new primary (S) three ways, from a fresh copy of it each time, both VMs' caches
# dropped:
#   rewind  the standard: pg_rewind from S, then B started as S's standby until it has replayed to S's LSN;
#   fast    pgdelta live, in place, --fast-path summaries --no-diverged-basis (its guard should refuse a diverged B,
#           so the session hashes both copies), then the copy started until it accepts connections;
#   full    one pg_basebackup -X stream into a new directory, then started the same way;
#   union   (EXP-71) pgdelta live, in place, --fast-path summaries --diverged-basis (idea 1: the blocks B's own WAL
#           names since the checkpoint before the fork, plus the summaries' since), with 8 reader threads, gaps of up
#           to 2 pages filled and the receiver's syncs on its worker, then started the same way. Its first run per
#           lag is checked with pg_amcheck --heapallindexed after the timing;
#   crashed (EXP-89b) union's settings with the receiver's --crashed-primary: B0's end proven by its own WAL
#           (research loop 4); with CRASHED_P=1 the only fast arm that can take B0. Every run is checked with
#           pg_amcheck --heapallindexed after the timing.
# CRASHED_P=1 (EXP-89b): at the fork P is not stopped cleanly: its dirty buffers are written out without a checkpoint
# (pg_buffercache_evict), so its pages past the fork are on disk, and it is stopped with -m immediate, a crash. B0 is
# then a primary left "in production"; pg_rewind runs crash recovery on it first, as it does for such a target.
# Each lag forks afresh. On the source VM a standby Q of the current primary P catches up and stops cleanly; P then
# writes 10,000 rows Q never sees and stops cleanly; P's data directory goes to dest as B0, and Q, promoted onto a new
# timeline, takes P's place as S. S then runs the lag: "hot10" (the same 1% hot rows updated 10 times) or "uniform"
# (1% of rows once). Each copy's pgbench_accounts must equal S's (B's own 10,000 rows undone).
#     setsid nohup bash experiments/network/lab_exp67.sh SOURCE_IP [REPEATS] [SEED] \
#         > /data/exp04-results/exp67-lab.log 2>&1 < /dev/null &
# SCALE (default 600, about 18 GB) and OUT change the size and the results directory (the smoke: SCALE=20).
# ARMS (default "rewind fast full") picks the timed arms. DIAG=1 (EXP-69) records, after each lag and before its
# runs, what pg_rewind would copy and idea 1's two block sets, in diag-LAG/: pg_rewind --dry-run --debug on a fresh
# copy of B0, S's data directory by top-level entry and pg_wal's files, B0's WAL from the last common checkpoint's
# redo (pg_waldump), and S's WAL summaries from that redo on (EXP-69: DIAG=1 ARMS=fast, 1 repeat).
set -u
SRC=$1
REPEATS=${2:-2}
SEED=${3:-670}
SCALE=${SCALE:-600}
ARMS=${ARMS:-rewind fast full}
DIAG=${DIAG:-0}
CRASHED_P=${CRASHED_P:-0}
DEST=$(hostname -I | awk '{print $1}')
cd ~/pgd
R=${OUT:-/data/exp04-results/exp67}
W=/data/exp67
rm -rf "$R" "$W"; mkdir -p "$R" "$W"
KEY=$HOME/.ssh/exp04_ed25519
SSRC=(ssh -n -i "$KEY" -p 2222 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$SRC")
export PATH="$PATH:/usr/sbin:/sbin"
PGBIN=/usr/lib/postgresql/17/bin
ROOT=$W/src
B0=$W/B0 B=$W/B FULL=$W/full
CONN="host=$ROOT/s port=5432 user=study dbname=postgres"
on_src() { "${SSRC[@]}" "export PATH=\$PATH:/usr/sbin:/sbin; cd ~/pgd; $1"; }
psrc() { on_src "$PGBIN/psql -h $ROOT/s -p 5432 -U study -d postgres -qAtX -v ON_ERROR_STOP=1 -c \"$1\""; }
pq() { on_src "$PGBIN/psql -h $W/qs -p 5434 -U study -d postgres -qAtX -c \"$1\"" 2>/dev/null; }
pcopy() { $PGBIN/psql -h /tmp -p 5433 -U study -d postgres -qAtX -c "$1" 2>/dev/null; }
drop() { sync; echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null; on_src "sync; echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null"; }
start_copy() {  # $1: data directory; started on 5433 without archiving or summaries; returns when it accepts connections
  $PGBIN/pg_ctl -D "$1" -l "$2" -w -t 3600 \
    -o "-p 5433 -k /tmp -c listen_addresses='' -c archive_mode=off -c summarize_wal=off" start > /dev/null 2>&1
}
stop_copy() { $PGBIN/pg_ctl -D "$1" -m fast -w stop > /dev/null 2>&1; }
# EXP-69 (DIAG=1): $1 the lag, $2 B0's timeline. Run before the lag's timed runs, with S idle.
diag() {
  local dd="$R/diag-$1" c redo
  mkdir -p "$dd"
  rm -rf "$B"; cp -a "$B0" "$B"
  $PGBIN/pg_rewind --target-pgdata="$B" --source-server="host=$SRC port=5432 user=study dbname=postgres" \
    --dry-run --debug --progress > "$dd/rewind-debug.log" 2>&1
  echo "rewind_exit $?" > "$dd/status.txt"
  rm -rf "$B"
  echo "s_lsn $(psrc "SELECT pg_current_wal_lsn()")" >> "$dd/status.txt"
  on_src "cd $ROOT/data && du -sk -- * | sort -k2 && ls -l pg_wal" > "$dd/source-listing.txt"
  c=$(sed -n 's/.*last common checkpoint at \([0-9A-F/]*\) on timeline.*/\1/p' "$dd/rewind-debug.log")
  redo=$($PGBIN/pg_waldump --path "$B0/pg_wal" --timeline "$2" --start "$c" -n 1 2>/dev/null \
    | sed -n 's/.*CHECKPOINT_[A-Z]* redo \([0-9A-F/]*\);.*/\1/p')
  echo "checkpoint $c redo $redo b_timeline $2" >> "$dd/status.txt"
  # Amended before part B ran (EXP-69): each relation file's size on both sides, and B0's own end.
  echo "b0_checkpoint $($PGBIN/pg_controldata "$B0" | sed -n 's/^Latest checkpoint location: *//p')" >> "$dd/status.txt"
  (cd "$B0" && find base global -type f -printf '%p %s\n') | gzip > "$dd/b0-files.txt.gz"
  on_src "cd $ROOT/data && find base global -type f -printf '%p %s\\n'" | gzip > "$dd/source-files.txt.gz"
  $PGBIN/pg_waldump --path "$B0/pg_wal" --timeline "$2" --start "$redo" 2>&1 | gzip > "$dd/b0-waldump.txt.gz"
  psrc "COPY (SELECT s.tli, s.start_lsn, s.end_lsn, c.reltablespace, c.reldatabase, c.relfilenode, c.relforknumber, c.relblocknumber, c.is_limit_block FROM pg_available_wal_summaries() s, pg_wal_summary_contents(s.tli, s.start_lsn, s.end_lsn) c WHERE s.end_lsn > '$redo') TO STDOUT WITH CSV" \
    | gzip > "$dd/s-summaries.csv.gz"
  psrc "COPY (SELECT tli, start_lsn, end_lsn FROM pg_available_wal_summaries() ORDER BY tli, start_lsn) TO STDOUT WITH CSV" \
    > "$dd/s-summary-ranges.csv"
  gzip -f "$dd/rewind-debug.log"
}
STATE="SELECT count(*) || ':' || sum(abalance) FROM pgbench_accounts"
CLUSTER_ARGS="--root $ROOT --bin-dir $PGBIN --log-dir $W/logs"
WORKLOAD_ARGS="$CLUSTER_ARGS --scale $SCALE --fraction 0.01 --seed 20261022 --hot-percent 1.0 --batch-size 100"

fork() {  # $1: the lag's name. Leaves B0 (the old primary) on dest and S (Q promoted) running as the source.
  on_src "rm -rf $W/q $W/qs && mkdir -p $W/qs && $PGBIN/pg_basebackup -h $ROOT/s -p 5432 -U study -D $W/q \
    -X stream -c fast -R > $W/q-basebackup.log 2>&1 && $PGBIN/pg_ctl -D $W/q -l $W/q.log -w -t 600 \
    -o \"-p 5434 -k $W/qs -c listen_addresses='' -c archive_mode=off\" start > /dev/null" \
    || { echo "the standby Q failed"; return 1; }
  local target; target=$(psrc "SELECT pg_current_wal_lsn()")
  for x in $(seq 600); do [ "$(pq "SELECT pg_last_wal_replay_lsn() >= '$target'")" = t ] && break; sleep 1; done
  on_src "$PGBIN/pg_ctl -D $W/q -m fast -w stop > /dev/null" || { echo "Q did not stop"; return 1; }
  # The fork: P writes what Q never sees, then stops cleanly (a fenced old primary).
  psrc "UPDATE pgbench_accounts SET abalance = abalance - 7 WHERE aid % 6000 = 3" > /dev/null \
    || { echo "the diverging update failed"; return 1; }
  if [ "$CRASHED_P" = 1 ]; then
    echo "{\"flushed_buffers\": $(psrc "SELECT count(*) FROM pg_buffercache WHERE isdirty AND pg_buffercache_evict(bufferid)")}" \
      > "$R/flush-$1.json"
    on_src "$PGBIN/pg_ctl -D $ROOT/data -m immediate -w stop > /dev/null" || { echo "P did not stop"; return 1; }
  else
    on_src "python3 experiments/network/source_cluster.py stop-clean --root $ROOT --bin-dir $PGBIN > $W/stop-$1.json" \
      || { echo "P did not stop cleanly"; return 1; }
  fi
  rm -rf "$B0"; mkdir -p "$B0"
  on_src "tar -C $ROOT/data -cf - ." | tar -C "$B0" -xf - || { echo "copying P to dest failed"; return 1; }
  # Q takes P's place, promoted onto a new timeline; its primary_conninfo (P's socket) goes first.
  on_src "rm -rf $ROOT/data && mv $W/q $ROOT/data && sed -i '/primary_conninfo/d' $ROOT/data/postgresql.auto.conf \
    && python3 experiments/network/source_cluster.py start $CLUSTER_ARGS > $W/start-$1.json \
    && $PGBIN/pg_ctl -D $ROOT/data -w -t 600 promote > /dev/null" || { echo "promoting Q failed"; return 1; }
  for x in $(seq 120); do [ "$(psrc "SELECT pg_is_in_recovery()")" = f ] && break; sleep 1; done
  psrc "CHECKPOINT" > /dev/null  # the new timeline in S's control file
  echo "{\"lag\": \"$1\", \"promoted_lsn\": \"$(psrc "SELECT pg_current_wal_lsn()")\", \"timeline\": $(psrc "SELECT timeline_id FROM pg_control_checkpoint()"), \"b0_state\": \"$($PGBIN/pg_controldata "$B0" | awk -F': *' '/cluster state/ {print $2}')\", \"b0_timeline\": $($PGBIN/pg_controldata "$B0" | awk -F': *' '/Latest checkpoint.s TimeLineID/ {print $2}')}" > "$R/fork-$1.json"
}

echo "$(date -u +%T) set-up: scale $SCALE"
on_src "mkdir -p $W && python3 experiments/network/source_cluster.py setup $WORKLOAD_ARGS --pattern hot \
  --listen-ip $SRC --client-ip $DEST > $W/setup.json" || { echo "set-up failed"; exit 1; }
# S keeps its WAL since the fork for pg_rewind and the rewound standby (a slot or an archive would do the same).
psrc "ALTER SYSTEM SET wal_keep_size = '40GB'" > /dev/null && psrc "SELECT pg_reload_conf()" > /dev/null
[ "$CRASHED_P" = 1 ] && psrc "CREATE EXTENSION IF NOT EXISTS pg_buffercache" > /dev/null
VERSION=$(psrc "SHOW server_version_num")
echo "{\"server_version_num\": $VERSION, \"summarize_wal\": \"$(psrc "SHOW summarize_wal")\", \"data_checksums\": \"$(psrc "SHOW data_checksums")\", \"database_bytes\": $(psrc "SELECT pg_database_size('postgres')"), \"scale\": $SCALE}" > "$R/source.json"
[ "$VERSION" -ge 170011 ] || { echo "the server is $VERSION: the fast path needs 17.11 or later; failed"; exit 1; }

i=0
for lag in hot10 uniform; do
  echo "$(date -u +%T) $lag: the fork (a standby promoted; the old primary to dest)"
  fork "$lag" || { echo "fork failed"; exit 1; }
  echo "$(date -u +%T) $lag: $(cat "$R/fork-$lag.json")"
  if [ "$lag" = hot10 ]; then passes=10; pattern=hot; else passes=1; pattern=uniform; fi
  for p in $(seq "$passes"); do
    on_src "python3 experiments/network/source_cluster.py workload $WORKLOAD_ARGS --pattern $pattern \
      > $W/workload-$lag-$p.json" || { echo "workload failed"; exit 1; }
  done
  S_LSN=$(psrc "SELECT pg_current_wal_lsn()")
  SOURCE_STATE=$(psrc "$STATE")
  # EXP-69: the state check reads all of pgbench_accounts after the lag's checkpoint, and with checksums each page's
  # first hint bit since then writes a full-page image (3.7 GB of WAL in EXP-67's uniform lag). S's LSN after it is
  # recorded too; the copies are made after it.
  S_LSN_CHECKED=$(psrc "SELECT pg_current_wal_lsn()")
  echo "{\"lag\": \"$lag\", \"source_lsn\": \"$S_LSN\", \"source_state\": \"$SOURCE_STATE\", \"source_lsn_after_check\": \"$S_LSN_CHECKED\"}" > "$R/lag-$lag.json"
  echo "$(date -u +%T) $lag: source at $S_LSN, state $SOURCE_STATE"
  if [ "$DIAG" = 1 ]; then
    diag "$lag" "$(python3 -c "import json; print(json.load(open('$R/fork-$lag.json'))['b0_timeline'])")"
    echo "$(date -u +%T) $lag: diagnostics recorded ($(head -1 "$R/diag-$lag/status.txt"))"
  fi
  python3 - "$REPEATS" "$SEED" "$lag" $ARMS > "$R/plan-$lag.txt" <<'EOF'
import random, sys
repeats, seed, lag, arms = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4:]
runs = [(arm, k) for arm in arms for k in range(repeats)]
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
    drop
    status=0 rstatus=null
    t0=$(date +%s.%N)
    case "$arm" in
      rewind)
        $PGBIN/pg_rewind --target-pgdata="$B" --source-server="host=$SRC port=5432 user=study dbname=postgres" \
          --write-recovery-conf --progress > "$d/rewind.log" 2>&1 || status=$?
        tr=$(date +%s.%N)
        [ "$status" = 0 ] && { start_copy "$B" "$d/copy.log" || status=$?; }
        for x in $(seq 9000); do [ "$(pcopy "SELECT pg_last_wal_replay_lsn() >= '$S_LSN'")" = t ] && break; sleep 0.2; done ;;
      fast)
        tr=null
        python3 -m pgdelta receive --basis "$B" --in-place --listen "$DEST:7000" \
          --basis-pg-controldata "$PGBIN/pg_controldata" --jobs 4 --fsync main > "$d/receive.log" 2>&1 &
        rp=$!
        for x in $(seq 200); do ss -ltn | grep -q ':7000 ' && break; sleep 0.05; done
        on_src "python3 -m pgdelta send --source $ROOT/data --source-kind live --pg-conn '$CONN' --connect $DEST:7000 \
          --pg-controldata $PGBIN/pg_controldata --jobs 4 --hash blake3 --fast-path summaries --read-ahead 0 --fill-gaps 0 \
          --no-diverged-basis --stats-json /tmp/exp67-send.json" > "$d/send.log" 2>&1 || status=$?
        wait "$rp"; rstatus=$?
        [ "$status" = 0 ] && { start_copy "$B" "$d/copy.log" || status=$?; }
        on_src "cat /tmp/exp67-send.json" > "$d/send.json" ;;
      union)
        tr=null
        python3 -m pgdelta receive --basis "$B" --in-place --listen "$DEST:7000" \
          --basis-pg-controldata "$PGBIN/pg_controldata" --jobs 4 --fsync worker > "$d/receive.log" 2>&1 &
        rp=$!
        for x in $(seq 200); do ss -ltn | grep -q ':7000 ' && break; sleep 0.05; done
        on_src "python3 -m pgdelta send --source $ROOT/data --source-kind live --pg-conn '$CONN' --connect $DEST:7000 \
          --pg-controldata $PGBIN/pg_controldata --jobs 4 --hash blake3 --fast-path summaries --diverged-basis \
          --read-ahead 8 --fill-gaps 2 --stats-json /tmp/exp67-send.json" > "$d/send.log" 2>&1 || status=$?
        wait "$rp"; rstatus=$?
        [ "$status" = 0 ] && { start_copy "$B" "$d/copy.log" || status=$?; }
        on_src "cat /tmp/exp67-send.json" > "$d/send.json" ;;
      crashed)
        tr=null
        python3 -m pgdelta receive --basis "$B" --in-place --listen "$DEST:7000" \
          --basis-pg-controldata "$PGBIN/pg_controldata" --jobs 4 --fsync worker --crashed-primary \
          > "$d/receive.log" 2>&1 &
        rp=$!
        for x in $(seq 200); do ss -ltn | grep -q ':7000 ' && break; sleep 0.05; done
        on_src "python3 -m pgdelta send --source $ROOT/data --source-kind live --pg-conn '$CONN' --connect $DEST:7000 \
          --pg-controldata $PGBIN/pg_controldata --jobs 4 --hash blake3 --fast-path summaries --diverged-basis \
          --read-ahead 8 --fill-gaps 2 --stats-json /tmp/exp67-send.json" > "$d/send.log" 2>&1 || status=$?
        wait "$rp"; rstatus=$?
        [ "$status" = 0 ] && { start_copy "$B" "$d/copy.log" || status=$?; }
        on_src "cat /tmp/exp67-send.json" > "$d/send.json" ;;
      full)
        tr=null
        target=$FULL
        $PGBIN/pg_basebackup -h "$SRC" -p 5432 -U study -D "$FULL" -X stream -c fast > "$d/full.log" 2>&1 || status=$?
        [ "$status" = 0 ] && { start_copy "$FULL" "$d/copy.log" || status=$?; } ;;
    esac
    t1=$(date +%s.%N)
    COPY_STATE=$(pcopy "$STATE")
    if { [ "$arm" = union ] && [ "$k" = 0 ]; } || [ "$arm" = crashed ]; then  # EXP-71, EXP-89b: every page, after
      $PGBIN/pg_amcheck -h /tmp -p 5433 -U study --all --heapallindexed --install-missing > "$d/amcheck.log" 2>&1
      echo $? > "$d/amcheck-status"
    fi
    stop_copy "$target"
    match=$([ "$COPY_STATE" = "$SOURCE_STATE" ] && echo true || echo false)
    printf '{"run": %s, "lag": "%s", "arm": "%s", "repeat": %s, "t0": %s, "t_rewound": %s, "t1": %s, "status": %s, "receiver_status": %s, "source_state": "%s", "copy_state": "%s", "match": %s}\n' \
      "$i" "$lag" "$arm" "$k" "$t0" "$tr" "$t1" "$status" "$rstatus" "$SOURCE_STATE" "$COPY_STATE" "$match" > "$d/meta.json"
    echo "$(date -u +%T) run $i: $lag $arm repeat $k: $(python3 -c "print(round($t1 - $t0, 2))") s, exit $status, match $match"
    i=$((i + 1))
  done < "$R/plan-$lag.txt"
done
rm -rf "$B" "$FULL" "$B0"
echo "$(date -u +%T) done"
