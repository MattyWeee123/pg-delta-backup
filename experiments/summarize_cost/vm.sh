#!/bin/bash
# EXP-83b, on dest of the ha set (docs/experiments/EXP-83b-summarize-wal-cost-vms.md): the source built at SCALE by
# source_cluster.py (EXP-48's database at scale 600, or smaller), then bench.py from here, the client host, against the
# source over the network, with the summarizer's CPU (summarizer_cpu.sh) and the source's /proc/stat read over ssh.
#     [SETTLE=S] [SKIP_SETUP=1] bash experiments/summarize_cost/vm.sh SOURCE_IP SCALE CLIENTS THREADS SECONDS PAIRS WARMUP \
#         SEED CELL OUT.csv
# SETTLE (EXP-83c) passes bench.py --settle; SKIP_SETUP=1 keeps the source the previous cell built.
set -eu
SRC=$1 SCALE=$2 CLIENTS=$3 THREADS=$4 SECONDS_=$5 PAIRS=$6 WARMUP=$7 SEED=$8 CELL=$9 OUT=${10}
cd ~/pgd
KEY=$HOME/.ssh/exp04_ed25519
SSRC="ssh -n -i $KEY -p 2222 -o BatchMode=yes -o StrictHostKeyChecking=accept-new $SRC"
PGBIN=/usr/lib/postgresql/17/bin ROOT=/data/exp64/src W=/data/exp64
DEST=$(hostname -I | awk '{print $1}')
if [ "${SKIP_SETUP:-0}" = 1 ]; then  # EXP-83c: the previous cell's source, at the same scale
  echo "$(date -u +%T) $CELL: the source as the previous cell left it"
else
  echo "$(date -u +%T) $CELL: set-up at scale $SCALE"
  $SSRC "cd ~/pgd && mkdir -p $W && python3 experiments/network/source_cluster.py setup --root $ROOT --bin-dir $PGBIN \
    --log-dir $W/logs --scale $SCALE --fraction 0.01 --seed 20261022 --hot-percent 1.0 --batch-size 100 --pattern hot \
    --listen-ip $SRC --client-ip $DEST > $W/setup-$CELL.json"
fi
echo "$(date -u +%T) $CELL: database $($SSRC "$PGBIN/psql -h $ROOT/s -U study -d postgres -qAtX -c \"SELECT pg_size_pretty(pg_database_size('postgres'))\"")"
python3 experiments/summarize_cost/bench.py --conn "host=$SRC port=5432 user=study dbname=postgres" --bin $PGBIN \
  --clients "$CLIENTS" --threads "$THREADS" --seconds "$SECONDS_" --pairs "$PAIRS" --warmup "$WARMUP" --seed "$SEED" \
  --settle "${SETTLE:-0}" --ps "$SSRC bash ~/pgd/experiments/summarize_cost/summarizer_cpu.sh" --stat "$SSRC cat /proc/stat" \
  --cell "$CELL" --out "$OUT"
echo "$(date -u +%T) $CELL: done"
