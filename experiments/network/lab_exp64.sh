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
# path with --read-ahead N (EXP-64b: PATTERNS=uniform ARMS="hash fast fast1 fast8 full"), "fastNw" the same with
# the receiver's --fsync worker, and "fastNwfK" with --fill-gaps K too (EXP-70). Every other arm syncs in the main
# loop and "fast" reads one run at a time, as when they were registered. ARMS_<pattern> (e.g. ARMS_hot) overrides ARMS for that pattern. Each run records
# /proc/diskstats for /data on both VMs just before t0 and just after t1 (diskstats.txt). PLAN=1 has every fast run
# write its plan (--plan-out); each run's digest of it goes in meta.json, and the first plan per pattern is kept as
# plan-runs-PATTERN.json.gz, and per arm plan-runs-PATTERN-ARM.json.gz (EXP-68: PATTERNS="hot uniform" ARMS_hot="fast fast8w"
# ARMS_uniform="fast8 fast16 fast8w fast16w full" PLAN=1). A pattern "scatterN" (EXP-74) changes N% of
# pgbench_accounts' rows, chosen by a hash of each row's key in one statement (the same rows whatever the table's
# physical order), since the uniform workload's batches of 100
# rows would take hours at such fractions: with about 27 rows a page, 1 - (1 - N/100)^27 of the heap's pages.
# A pattern "scatterallN" (EXP-75) changes N% of events' rows the same way too (payload upper-cased, so its length is
# unchanged), so nearly every relation of the database changes; each copy's check then counts the events changed.
# An arm "fastNws" (EXP-75) is fastNw with --selector instead of a fill: it chooses its fill, or a full copy.
# A "b" in place of the "w" (EXP-76) syncs on the worker with early write-back (--fsync worker-writeback). SERIES=1
# samples both VMs' /data disk every 0.25 s around each run (disk_series.sh: series-dest.txt, series-source.txt).
# A final "q" (EXP-77) has the receiver verify in its main loop (--verify sequential), where the fast path's written
# pages are then re-read; without it they are re-read on the pipelined verifier (the default since EXP-77).
# CRASHED=1 (EXP-80b): each pattern's B0 is a standby instead (pg_basebackup -R), started on dest before the change and
# killed with pg_ctl -m immediate KILL_AFTER seconds (default 30) into it, while the change goes on to its end on the
# source; and a final "c" on a fast arm passes the receiver --crashed-standby (every other arm --no-crashed-standby, its
# meaning before K15 made it the default). Each copy's check removes standby.signal.
# EXP-81, the competitors in their best modes, with dest's /data on XFS with reflinks (reformat_dest_xfs.sh):
# - "incr": native incremental, pg_basebackup --incremental against B0's manifest (B0 itself, never started: a
#   backup chain; the capture synced, as by default), then pg_combinebackup --copy-file-range (each reused block
#   shared, not copied, on XFS) into a new directory, synced by syncfs; its output is the copy checked.
# - STANDBY_TOO=1: each pattern also gets B0s, a standby (pg_basebackup -R) started, caught up and stopped cleanly
#   before the change; an arm "sb_X" runs arm X on a copy of B0s instead of B0.
# - "walg" (on B0s: "sb_walg"): WAL-G's catch-up, catchup-receive on dest and catchup-send on the source, then a
#   sync of /data, since WAL-G's receiver syncs nothing; its copy is checked as the standby it stays, once it has
#   replayed to the source's current LSN.
# EXP-82b, summaries off (research loop 3): SUMMARIES=0 turns the source's summarize_wal off (ALTER SYSTEM, which
# overrides run_study.py's base postgresql.conf, where it is on for every VM source since EXP-64: EXP-82b's source
# summarized throughout, found after its run), and checks the summarizer has never run; WAL_KEEP (e.g. 32GB)
# sets its wal_keep_size, so the WAL since each old copy stays on disk, as a replication slot would keep it. Arms:
# - "hash": no fast path (pgdelta hashes both copies);
# - "lsn": --fast-path page-lsn, with the fast path's other defaults (2-page gaps filled, the fsync worker);
# - "lsn1" (EXP-85): "lsn" with --lsn-single-read, sending the chosen pages from the scan's reads;
# - "od" (summaries on demand): summarize_wal turned on (ALTER SYSTEM, a reload) and the summarizer waited for until it
#   has read the WAL up to the source's LSN at that moment, then the summaries' fast path with its defaults; timed from
#   before the setting is turned on. Afterwards it is turned off again and its summaries removed, so each run starts
#   from a server that never summarized (the setting is set off, not reset, since a reset falls back to the base
#   postgresql.conf's on).
# SLOT=1: before each pattern's old copies are taken, a physical replication slot (exp82b) is reserved on the source,
# so it keeps exactly the WAL since their redo points (a checkpoint recycles every older segment): the summarizer, which
# starts at the oldest WAL on disk, then reads only that. wal_keep_size would keep older WAL too. Dropped at the end.
# Every pgdelta arm's copy is checked (the hash arm's too, since EXP-82b).
# EXP-84 (the held-out session): PG_MAJOR picks the PostgreSQL major the lab runs (installed beside 17), and
# FULL_CHECK=1 has each copy's check compare, beyond pgbench_accounts, the events table (count and the sum of its ids)
# and pgbench's branches, tellers and history, since the held-out patterns tpcb, append and retention change them.
# Every copy of B0 or B0s is a real one (cp --reflink=never: on XFS cp would otherwise share blocks with B0), and each
# run records dest's received network bytes (net_rx_bytes) and, for incr and walg, its phases' seconds.
set -u
SRC=$1
REPEATS=${2:-2}
SEED=${3:-640}
SCALE=${SCALE:-600}
PATTERNS=${PATTERNS:-hot uniform}
ARMS=${ARMS:-hash fast full}
PLAN=${PLAN:-0}
SERIES=${SERIES:-0}
CRASHED=${CRASHED:-0}
KILL_AFTER=${KILL_AFTER:-30}
STANDBY_TOO=${STANDBY_TOO:-0}
SUMMARIES=${SUMMARIES:-1}
SLOT=${SLOT:-0}
WAL_KEEP=${WAL_KEEP:-}
WALG=${WALG:-/usr/local/bin/wal-g}
DEST=$(hostname -I | awk '{print $1}')
cd ~/pgd
R=${OUT:-/data/exp04-results/exp64}
W=/data/exp64
rm -rf "$R" "$W"; mkdir -p "$R" "$W"
KEY=$HOME/.ssh/exp04_ed25519
# -n: inside the plan loop ssh must not read the plan from stdin. The source on the control port (outside wan.sh).
SSRC=(ssh -n -i "$KEY" -p 2222 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$SRC")
export PATH="$PATH:/usr/sbin:/sbin"
PGBIN=/usr/lib/postgresql/${PG_MAJOR:-17}/bin  # PG_MAJOR (EXP-84, held-out H4): another installed major
ROOT=/data/exp64/src
B0=$W/B0 B=$W/B FULL=$W/full B0S=$W/B0s INC=$W/inc
NETIF=$(ip route show default | awk '{print $5; exit}')
netrx() { awk -v i="$NETIF:" '$1 == i {print $2}' /proc/net/dev; }
pcopy() { $PGBIN/psql -h /tmp -p "$1" -U study -d postgres -qAtX -c "$2" 2>&1; }
# Wait until the server on port $1 has replayed to the source's current LSN (at most 600 s).
caught_up() {
  local target; target=$(psrc "SELECT pg_current_wal_lsn()")
  for x in $(seq 600); do [ "$(pcopy "$1" "SELECT pg_last_wal_replay_lsn() >= '$target'")" = t ] && return 0; sleep 1; done
  return 1
}
CONN="host=$ROOT/s port=5432 user=study dbname=postgres"
on_src() { "${SSRC[@]}" "export PATH=\$PATH:/usr/sbin:/sbin; cd ~/pgd; $1"; }
psrc() { on_src "$PGBIN/psql -h $ROOT/s -p 5432 -U study -d postgres -qAtX -v ON_ERROR_STOP=1 -c \"$1\""; }
drop() { sync; echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null; on_src "sync; echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null"; }
# The /proc/diskstats line of the device behind /data, on dest and on the source.
DISK='awk -v d="$(basename "$(findmnt -no SOURCE /data)")" '"'"'$3 == d'"'"' /proc/diskstats'
disks() { echo "dest $(eval "$DISK")"; echo "source $(on_src "$DISK")"; }
STATE="SELECT count(*) || ':' || sum(abalance) FROM pgbench_accounts"
WORKLOAD_ARGS="--root $ROOT --bin-dir $PGBIN --log-dir $W/logs --scale $SCALE --fraction 0.01 --seed 20261022 --hot-percent 1.0 --batch-size 100"

echo "$(date -u +%T) set-up: scale $SCALE"
# setup stops a cluster left at ROOT (the smoke's) before it clears ROOT.
on_src "mkdir -p $W && python3 experiments/network/source_cluster.py setup $WORKLOAD_ARGS --pattern hot \
  --listen-ip $SRC --client-ip $DEST > $W/setup.json" || { echo "set-up failed"; exit 1; }
# Summaries from here on: on before any old copy is taken, so they cover each B0's start onwards.
if [ "$SUMMARIES" = 1 ]; then
  on_src "echo 'summarize_wal = on  # EXP-64' >> $ROOT/data/postgresql.conf && $PGBIN/pg_ctl -D $ROOT/data reload > /dev/null"
fi
if [ "$SUMMARIES" = 0 ]; then
  # The base postgresql.conf had it on through the load: off now, a restart (its shared state cleared), and the
  # summaries made so far removed, so the source is as one that never summarized.
  psrc "ALTER SYSTEM SET summarize_wal = off" > /dev/null
  on_src "$PGBIN/pg_ctl -D $ROOT/data -m fast -w -t 600 restart > /dev/null && rm -f $ROOT/data/pg_wal/summaries/*.summary" \
    || { echo "source restart failed"; exit 1; }
  [ "$(psrc "SHOW summarize_wal")" = off ] || { echo "summarize_wal is still on: failed"; exit 1; }
  [ "$(psrc "SELECT summarized_lsn = '0/0' AND summarizer_pid IS NULL FROM pg_get_wal_summarizer_state()")" = t ] || \
    { echo "the source has already summarized WAL: failed"; exit 1; }
  [ -z "$(on_src "ls $ROOT/data/pg_wal/summaries")" ] || { echo "the source holds WAL summaries: failed"; exit 1; }
fi
if [ -n "$WAL_KEEP" ]; then
  psrc "ALTER SYSTEM SET wal_keep_size = '$WAL_KEEP'" > /dev/null && psrc "SELECT pg_reload_conf()" > /dev/null
fi
psrc "CHECKPOINT"
for x in $(seq 120); do
  [ "$SUMMARIES" = 1 ] || break
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
  if [ "$SLOT" = 1 ]; then
    psrc "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name = 'exp82b'" > /dev/null
    psrc "SELECT pg_create_physical_replication_slot('exp82b', true)" > /dev/null || { echo "slot failed"; exit 1; }
    echo "$(date -u +%T) $pattern: slot exp82b reserved at $(psrc "SELECT restart_lsn FROM pg_replication_slots WHERE slot_name = 'exp82b'")"
  fi
  rm -rf "$B0"
  if [ "$CRASHED" = 1 ]; then
    echo "$(date -u +%T) $pattern: B0, a standby of the source, started before the change"
    $PGBIN/pg_basebackup -h "$SRC" -p 5432 -U study -D "$B0" -R -X stream -c fast > "$R/B0-$pattern.log" 2>&1 \
      || { echo "B0 failed"; exit 1; }
    $PGBIN/pg_ctl -D "$B0" -l "$R/standby-$pattern.log" -w -t 300 \
      -o "-p 5434 -k /tmp -c listen_addresses='' -c summarize_wal=off" start > /dev/null 2>&1 || { echo "standby failed"; exit 1; }
  else
    echo "$(date -u +%T) $pattern: B0, a base backup before the change"
    $PGBIN/pg_basebackup -h "$SRC" -p 5432 -U study -D "$B0" -X stream -c fast > "$R/B0-$pattern.log" 2>&1 \
      || { echo "B0 failed"; exit 1; }
  fi
  if [ "$STANDBY_TOO" = 1 ]; then
    rm -rf "$B0S"
    $PGBIN/pg_basebackup -h "$SRC" -p 5432 -U study -D "$B0S" -R -X stream -c fast > "$R/B0s-$pattern.log" 2>&1 \
      || { echo "B0s failed"; exit 1; }
    # Port 5435: with CRASHED=1, B0's standby is still running on 5434 (EXP-84 found the two modes never combined).
    $PGBIN/pg_ctl -D "$B0S" -l "$R/standby-$pattern.log" -w -t 300 \
      -o "-p 5435 -k /tmp -c listen_addresses='' -c summarize_wal=off" start > /dev/null 2>&1 || { echo "B0s failed"; exit 1; }
    caught_up 5435 || { echo "B0s did not catch up"; exit 1; }
    $PGBIN/pg_ctl -D "$B0S" -m fast -w stop > /dev/null 2>&1
    echo "$(date -u +%T) $pattern: B0s, a standby caught up and stopped before the change: $($PGBIN/pg_controldata "$B0S" | \
      grep -E 'cluster state' | sed 's/.*: *//')"
  fi
  CHECK=$STATE
  [[ "$pattern" =~ ^scatterall[0-9]+$ ]] && \
    CHECK="SELECT ($STATE) || ':' || (SELECT count(*) FROM events WHERE payload <> lower(payload))"
  [ "${FULL_CHECK:-0}" = 1 ] && \
    CHECK="SELECT ($STATE) || ':' || (SELECT count(*) || ':' || coalesce(sum(id), 0) FROM events) || ':' || \
(SELECT sum(bbalance) FROM pgbench_branches) || ':' || (SELECT sum(tbalance) FROM pgbench_tellers) || ':' || \
(SELECT count(*) FROM pgbench_history)"
  change() {
    if [[ "$pattern" =~ ^scatter(all)?([0-9]+)$ ]]; then
      n=${BASH_REMATCH[2]}
      frac=$(python3 -c "print($n / 100)")
      w0=$(date +%s.%N)
      psrc "UPDATE pgbench_accounts SET abalance = abalance + 1 WHERE abs(hashint4(aid + 74)) % 10000 < ${n}00" \
        > /dev/null || { echo "workload failed"; return 1; }
      if [ -n "${BASH_REMATCH[1]}" ]; then
        psrc "UPDATE events SET payload = upper(payload) WHERE abs(hashint8(id + 74)) % 10000 < ${n}00" \
          > /dev/null || { echo "workload failed"; return 1; }
      fi
      w1=$(date +%s.%N)
      echo "{\"pattern\": \"$pattern\", \"fraction\": $frac, \"seconds\": $(python3 -c "print(round($w1 - $w0, 1))")}" \
        > "$R/workload-$pattern.json"
    else
      on_src "python3 experiments/network/source_cluster.py workload $WORKLOAD_ARGS --pattern $pattern \
        > $W/workload-$pattern.json" || { echo "workload failed"; return 1; }
      on_src "cat $W/workload-$pattern.json" > "$R/workload-$pattern.json"
    fi
  }
  if [ "$CRASHED" = 1 ]; then
    change & cp_pid=$!
    sleep "$KILL_AFTER"
    $PGBIN/pg_ctl -D "$B0" -w -m immediate stop > /dev/null 2>&1
    rm -f "$B0/postmaster.pid"  # stale after an immediate stop, if left: no process is left on it
    echo "$(date -u +%T) $pattern: the standby killed $KILL_AFTER s into the change: $($PGBIN/pg_controldata "$B0" | \
      grep -E 'cluster state' | sed 's/.*: *//')"
    wait "$cp_pid" || { echo "workload failed"; exit 1; }
  else
    change || exit 1
  fi
  SOURCE_STATE=$(psrc "$CHECK")
  echo "$(date -u +%T) $pattern: workload done; source state $SOURCE_STATE"
  arms_var="ARMS_$pattern"; arms=${!arms_var:-$ARMS}
  python3 - "$REPEATS" "$SEED" "$pattern" $arms > "$R/plan-$pattern.txt" <<'EOF'
import random, sys
repeats, seed, pattern, arms = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4:]
runs = [(arm, k) for arm in arms for k in range(repeats)]
digits = ''.join(c for c in pattern if c.isdigit())  # scatterN and scatterallN: N
random.Random(seed + (pattern == 'uniform') + (int(digits) if pattern.startswith('scatter') else 0)).shuffle(runs)
for arm, k in runs:
    print(arm, k)
EOF
  while read -r arm k; do
    d="$R/run-$(printf %02d "$i")"
    mkdir -p "$d"
    rm -rf "$B" "$FULL" "$INC"
    copied=null base=$B0 a=$arm
    [ "${arm#sb_}" != "$arm" ] && base=$B0S a=${arm#sb_}
    if [ "$a" != full ] && [ "$a" != incr ]; then
      c0=$(date +%s.%N); cp -a --reflink=never "$base" "$B"; c1=$(date +%s.%N)
      copied=$(python3 -c "print(round($c1 - $c0, 2))")
    fi
    drop
    disks | sed 's/^/before /' > "$d/diskstats.txt"
    if [ "$SERIES" = 1 ]; then
      bash experiments/network/disk_series.sh > "$d/series-dest.txt" 2> /dev/null & sp=$!
      on_src "nohup bash experiments/network/disk_series.sh > /tmp/exp64-series.txt 2> /dev/null < /dev/null & echo \$! > /tmp/exp64-series.pid"
      sleep 0.5  # a few samples before t0
    fi
    rstatus=null status= plan=null phases=null
    rx0=$(netrx)
    if [ "$a" = incr ]; then
      t0=$(date +%s.%N)
      # Synced, as by default: from an unsynced capture, XFS writes each changed block out before it can share it,
      # one block at a time, and the combine took 8.6 s instead of 0.75 s (EXP-81's pilot, scale 20).
      $PGBIN/pg_basebackup -h "$SRC" -p 5432 -U study -D "$INC" --incremental="$B0/backup_manifest" -X stream -c fast \
        > "$d/incr.log" 2>&1
      status=$?
      ti=$(date +%s.%N)
      [ "$status" = 0 ] && { $PGBIN/pg_combinebackup "$B0" "$INC" -o "$FULL" --copy-file-range --manifest-checksums=NONE \
        --sync-method=syncfs >> "$d/incr.log" 2>&1; status=$?; }
      t1=$(date +%s.%N)
      ib=$(du -sb "$INC" 2> /dev/null | cut -f1)
      phases="{\"capture_s\": $(python3 -c "print(round($ti - $t0, 3))"), \"combine_s\": $(python3 -c "print(round($t1 - $ti, 3))"), \"incremental_bytes\": ${ib:-null}}"
    elif [ "$a" = walg ]; then
      $WALG catchup-receive "$B" 7000 > "$d/walg-receive.log" 2>&1 &
      rp=$!
      for x in $(seq 200); do ss -ltn | grep -q ':7000 ' && break; sleep 0.05; done
      t0=$(date +%s.%N)
      on_src "PGHOST=$ROOT/s PGPORT=5432 PGUSER=study PGDATABASE=postgres WALG_FILE_PREFIX=/tmp/walg-unused \
        $WALG catchup-send $ROOT/data $DEST:7000" > "$d/walg-send.log" 2>&1
      status=$?
      [ "$status" = 0 ] || kill "$rp" 2> /dev/null  # a sender that never connected leaves the receiver waiting
      wait "$rp"; rstatus=$?
      ts=$(date +%s.%N)
      sync -f "$B"
      t1=$(date +%s.%N)
      phases="{\"catchup_s\": $(python3 -c "print(round($ts - $t0, 3))"), \"sync_s\": $(python3 -c "print(round($t1 - $ts, 3))")}"
    elif [ "$a" = full ]; then
      t0=$(date +%s.%N)
      $PGBIN/pg_basebackup -h "$SRC" -p 5432 -U study -D "$FULL" -X stream -c fast > "$d/full.log" 2>&1
      status=$?
      t1=$(date +%s.%N)
    else
      # Every setting explicit, since EXP-68 made 8 reader threads and the sync worker the fast path's defaults:
      # "fast" reads one run at a time and syncs in the main loop, as in EXP-64; fastN reads on N threads; a "w"
      # syncs on the worker; "fK" fills clean gaps of up to K pages (EXP-70's --fill-gaps), and no "fK" fills none,
      # since EXP-70 made 2 pages the default; none takes an old primary from its own WAL (EXP-71's default).
      # A final "s" has --selector choose the fill, or a full copy, instead (EXP-75). A crashed standby is taken only
      # by a final "c" (--crashed-standby), as when it was opt-in: K15 made it the default on October 30.
      fast= fsync='--fsync main' crashed=--no-crashed-standby
      if [[ "$a" =~ ^fast([0-9]*)([wb]?)(f([0-9]+)|s)?(q?)(c?)$ ]]; then
        fill="--fill-gaps ${BASH_REMATCH[4]:-0}"
        [ "${BASH_REMATCH[3]}" = s ] && fill=--selector
        fast="--fast-path summaries --read-ahead ${BASH_REMATCH[1]:-0} $fill --no-diverged-basis"
        [ "${BASH_REMATCH[2]}" = w ] && fsync='--fsync worker'
        [ "${BASH_REMATCH[2]}" = b ] && fsync='--fsync worker-writeback'
        [ "${BASH_REMATCH[5]}" = q ] && fsync="$fsync --verify sequential"
        [ "${BASH_REMATCH[6]}" = c ] && crashed=--crashed-standby
      fi
      case "$a" in
        lsn) fast="--fast-path page-lsn --fill-gaps 2 --no-diverged-basis" fsync='--fsync worker' ;;
        lsn1) fast="--fast-path page-lsn --lsn-single-read --fill-gaps 2 --no-diverged-basis" fsync='--fsync worker' ;;
        od) fast="--fast-path summaries --read-ahead 8 --fill-gaps 2 --no-diverged-basis" fsync='--fsync worker' ;;
      esac
      [ "$PLAN" = 1 ] && [ -n "$fast" ] && fast="$fast --plan-out /tmp/exp64-plan.json" && on_src "rm -f /tmp/exp64-plan.json"
      python3 -m pgdelta receive --basis "$B" --in-place --listen "$DEST:7000" \
        --basis-pg-controldata "$PGBIN/pg_controldata" --jobs 4 $fsync $crashed > "$d/receive.log" 2>&1 &
      rp=$!
      for x in $(seq 200); do ss -ltn | grep -q ':7000 ' && break; sleep 0.05; done
      t0=$(date +%s.%N)
      if [ "$a" = od ]; then
        target=$(psrc "SELECT pg_current_wal_lsn()")
        psrc "ALTER SYSTEM SET summarize_wal = on" > /dev/null && psrc "SELECT pg_reload_conf()" > /dev/null
        for x in $(seq 7200); do
          [ "$(psrc "SELECT coalesce(pending_lsn >= '$target', false) FROM pg_get_wal_summarizer_state()")" = t ] && break
          sleep 0.25
        done
        ts=$(date +%s.%N)
      fi
      on_src "python3 -m pgdelta send --source $ROOT/data --source-kind live --pg-conn '$CONN' --connect $DEST:7000 \
        --pg-controldata $PGBIN/pg_controldata --jobs 4 --hash blake3 $fast --stats-json /tmp/exp64-send.json" \
        > "$d/send.log" 2>&1
      status=$?
      wait "$rp"; rstatus=$?
      t1=$(date +%s.%N)
      on_src "cat /tmp/exp64-send.json" > "$d/send.json"
      if [ "$a" = od ]; then
        phases="{\"summarize_s\": $(python3 -c "print(round($ts - $t0, 3))"), \"refresh_s\": $(python3 -c "print(round($t1 - $ts, 3))"), \"wal_from\": \"$target\"}"
        psrc "ALTER SYSTEM SET summarize_wal = off" > /dev/null && psrc "SELECT pg_reload_conf()" > /dev/null
        for x in $(seq 100); do
          [ "$(psrc "SELECT summarizer_pid IS NULL FROM pg_get_wal_summarizer_state()")" = t ] && break; sleep 0.1
        done
        on_src "rm -f $ROOT/data/pg_wal/summaries/*.summary"
      fi
    fi
    disks | sed 's/^/after /' >> "$d/diskstats.txt"
    rx1=$(netrx)
    if [ "$SERIES" = 1 ]; then
      sleep 0.5  # a few samples after t1
      kill "$sp"; wait "$sp" 2> /dev/null
      on_src 'kill $(cat /tmp/exp64-series.pid); sleep 0.3; cat /tmp/exp64-series.txt; rm -f /tmp/exp64-series.txt /tmp/exp64-series.pid' \
        > "$d/series-source.txt"
    fi
    if [ "$PLAN" = 1 ] && [ "${arm#fast}" != "$arm" ] && on_src "test -s /tmp/exp64-plan.json"; then
      plan="\"$(on_src "sha256sum /tmp/exp64-plan.json" | cut -c1-16)\""
      [ -s "$R/plan-runs-$pattern.json.gz" ] || on_src "gzip -c /tmp/exp64-plan.json" > "$R/plan-runs-$pattern.json.gz"
      # EXP-70: the first plan per pattern and arm too, since --fill-gaps changes the plan.
      [ -s "$R/plan-runs-$pattern-$arm.json.gz" ] || on_src "gzip -c /tmp/exp64-plan.json" > "$R/plan-runs-$pattern-$arm.json.gz"
    fi
    verify=null
    if [ "$a" != full ] && [ "$status" = 0 ]; then
      # The refreshed copy recovers from its backup_label; its pgbench_accounts must equal the source's. incr's copy
      # is its new directory. A fast copy of a standby is started as a server of its own (no standby.signal); walg's
      # stays a standby, as WAL-G's catch-up leaves it, and is read once it has replayed to the source's LSN.
      cdir=$B
      [ "$a" = incr ] && cdir=$FULL
      [ "$a" = walg ] || rm -f "$cdir/standby.signal"
      $PGBIN/pg_ctl -D "$cdir" -l "$d/copy.log" -w -t 1800 \
        -o "-p 5433 -k /tmp -c listen_addresses='' -c summarize_wal=off" start > /dev/null 2>&1
      [ "$a" = walg ] && { caught_up 5433 || echo "walg's standby did not catch up" >> "$d/copy.log"; }
      COPY_STATE=$(pcopy 5433 "$CHECK")
      $PGBIN/pg_ctl -D "$cdir" -m fast -w stop > /dev/null 2>&1
      verify="{\"source\": \"$SOURCE_STATE\", \"copy\": \"$COPY_STATE\", \"match\": $([ "$SOURCE_STATE" = "$COPY_STATE" ] && echo true || echo false)}"
    fi
    printf '{"run": %s, "pattern": "%s", "arm": "%s", "repeat": %s, "t0": %s, "t1": %s, "status": %s, "receiver_status": %s, "copy_seconds": %s, "verify": %s, "plan": %s, "phases": %s, "net_rx_bytes": %s}\n' \
      "$i" "$pattern" "$arm" "$k" "$t0" "$t1" "$status" "$rstatus" "$copied" "$verify" "$plan" "$phases" "$((rx1 - rx0))" > "$d/meta.json"
    echo "$(date -u +%T) run $i: $pattern $arm repeat $k: $(python3 -c "print(round($t1 - $t0, 2))") s, exit $status" \
      "$([ "$verify" != null ] && python3 -c "import json,sys; print('match', json.loads(sys.argv[1])['match'])" "$verify")"
    i=$((i + 1))
  done < "$R/plan-$pattern.txt"
done
rm -rf "$B" "$FULL" "$B0" "$B0S" "$INC"
[ "$SLOT" = 1 ] && psrc "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name = 'exp82b'" > /dev/null
echo "$(date -u +%T) done"
