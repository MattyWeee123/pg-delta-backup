#!/usr/bin/env bash
# The 2-minute quick start: a real PostgreSQL cluster on your laptop, refreshed by pgdelta, then started and checked.
#
#   bash examples/local_demo.sh [WORK_DIR]            # default WORK_DIR: ./demo-work (deleted and recreated)
#   CHANGE=uniform SCALE=50 bash examples/local_demo.sh
#
# Steps: build S (pgbench, SCALE x 15 MB), copy it to B (the old copy), change rows on S, then
#   1. pg_basebackup copies all of S (the standard tool, for comparison);
#   2. pgdelta refreshes B into O, sending only the chunks that differ;
#   3. O is started and its rows compared with S's.
# CHANGE=hot (default) updates 1% of the rows in one contiguous range, so few pages change.
# CHANGE=uniform updates every 100th row, which touches nearly every page of the table.
# Needs PostgreSQL 17 or 18 binaries: PGBIN, else pg_config's bindir, else Homebrew's postgresql@17.
# Uses TCP on 127.0.0.1 ports 55432 and 55433, and no Unix sockets.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mkdir -p "${1:-demo-work}" && cd "${1:-demo-work}" && pwd)
SCALE=${SCALE:-20}
CHANGE=${CHANGE:-hot}
PYTHON=${PYTHON:-python3}
if [ -z "${PGBIN:-}" ]; then
  if command -v pg_config > /dev/null; then PGBIN=$(pg_config --bindir); else PGBIN=/opt/homebrew/opt/postgresql@17/bin; fi
fi
export LC_ALL=${LC_ALL:-en_US.UTF-8} LANG=${LANG:-en_US.UTF-8}
OPTS="-c listen_addresses=127.0.0.1 -c unix_socket_directories=''"
psql() { "$PGBIN/psql" -h 127.0.0.1 -U postgres -qAt "$@" postgres; }
start() { "$PGBIN/pg_ctl" -D "$1" -o "-p $2 $OPTS" -l "$WORK/$(basename "$1").log" -w start > /dev/null; }
stop() { "$PGBIN/pg_ctl" -D "$1" -m fast -w stop > /dev/null; }
seconds() { "$PYTHON" -c 'import sys, time; print(f"{time.time() - float(sys.argv[1]):.2f}")' "$1"; }
now() { "$PYTHON" -c 'import time; print(time.time())'; }
trap 'for d in S O; do [ -f "$WORK/$d/postmaster.pid" ] && stop "$WORK/$d"; done; true' EXIT

rm -rf "${WORK:?}"/S "$WORK"/B "$WORK"/O "$WORK"/full
echo "== PostgreSQL $("$PGBIN/postgres" --version | awk '{print $3}'), scale $SCALE, change $CHANGE, in $WORK"
"$PGBIN/initdb" -D "$WORK/S" -U postgres --data-checksums -A trust > "$WORK/initdb.log"
start "$WORK/S" 55432
"$PGBIN/pgbench" -i -s "$SCALE" -h 127.0.0.1 -p 55432 -U postgres postgres > "$WORK/pgbench.log" 2>&1
stop "$WORK/S"
cp -a "$WORK/S" "$WORK/B"
echo "S and its old copy B: $(du -sh "$WORK/S" | cut -f1) each"

start "$WORK/S" 55432
ROWS=$((SCALE * 100000))
if [ "$CHANGE" = uniform ]; then WHERE='aid % 100 = 0'; else WHERE="aid <= $((ROWS / 100))"; fi
psql -p 55432 -c "UPDATE pgbench_accounts SET abalance = abalance + 7 WHERE $WHERE" -c 'CHECKPOINT' > /dev/null
EXPECT=$(psql -p 55432 -c 'SELECT count(*), sum(abalance) FROM pgbench_accounts')
echo "changed 1% of $ROWS rows ($CHANGE)"

t0=$(now)
"$PGBIN/pg_basebackup" -h 127.0.0.1 -p 55432 -U postgres -D "$WORK/full" -X stream -c fast
echo "1. pg_basebackup copied all of S: $(du -sh "$WORK/full" | cut -f1) in $(seconds "$t0") s"
stop "$WORK/S"

echo "2. pgdelta refreshes B into O:"
(cd "$REPO" && "$PYTHON" -m pgdelta local --source "$WORK/S" --basis "$WORK/B" --output "$WORK/O" \
  --stats-json "$WORK/pgdelta-stats.json")

start "$WORK/O" 55433
GOT=$(psql -p 55433 -c 'SELECT count(*), sum(abalance) FROM pgbench_accounts')
stop "$WORK/O"
echo "3. O started: rows and balance $GOT, source $EXPECT"
if [ -n "$EXPECT" ] && [ "$EXPECT" = "$GOT" ]; then echo "   MATCH"; else echo "   MISMATCH"; exit 1; fi
