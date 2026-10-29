#!/bin/bash
# EXP-83's local stage (docs/experiments/EXP-83-summarize-wal-cost.md): a fresh local PostgreSQL 17 (data checksums,
# the VMs' WAL settings: checkpoint_timeout 30 min, max_wal_size 16 GB; shared_buffers 128 MB), pgbench -i at SCALE,
# then bench.py on it with the Mac's ps for the summarizer's CPU. Everything is removed afterwards.
#     bash experiments/summarize_cost/local.sh OUT.csv [SCALE] [CLIENTS] [THREADS] [SECONDS] [PAIRS] [WARMUP] [SEED]
set -eu
OUT=$1 SCALE=${2:-50} CLIENTS=${3:-16} THREADS=${4:-4} SECONDS_=${5:-30} PAIRS=${6:-6} WARMUP=${7:-30} SEED=${8:-830}
BIN=${PG_BIN:-/opt/homebrew/opt/postgresql@17/bin}
export LC_ALL=C
W=$(mktemp -d /tmp/exp83.XXXX) SOCK=$(mktemp -d /tmp/exp83s.XXXX) PORT=5481
trap '$BIN/pg_ctl -D $W/S -m immediate stop > /dev/null 2>&1; rm -rf $W $SOCK' EXIT
$BIN/initdb -D $W/S -U t -A trust --no-sync --data-checksums > /dev/null
printf "checkpoint_timeout = 30min\nmax_wal_size = 16GB\nshared_buffers = 128MB\nsummarize_wal = off\n" >> $W/S/postgresql.conf
$BIN/pg_ctl -D $W/S -l $W/S.log -w -o "-p $PORT -k $SOCK -c listen_addresses=''" start > /dev/null
$BIN/pgbench -i -q -s "$SCALE" -h $SOCK -p $PORT -U t postgres > /dev/null 2>&1
cd "$(dirname "$0")/../.."
python3 experiments/summarize_cost/bench.py --conn "host=$SOCK port=$PORT user=t dbname=postgres" --bin $BIN \
  --clients "$CLIENTS" --threads "$THREADS" --seconds "$SECONDS_" --pairs "$PAIRS" --warmup "$WARMUP" --seed "$SEED" \
  --ps "ps -axo time,command" --cell "local-s$SCALE-c$CLIENTS" --out "$OUT"
