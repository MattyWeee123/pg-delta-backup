#!/bin/bash
# EXP-90, run on each VM of the ha set (docs/experiments/EXP-90-where-hashing-time-goes.md): can this VM's /data disk
# read and write at once, or do reads and writes share one budget? fio with direct I/O on a 20 GiB file: 1 MiB
# sequential reads at queue depth 8 (reading B), 8 KiB random writes at queue depth 32 (writing scattered pages),
# each alone and then both at once; and, alone, 1 MiB sequential writes and 8 KiB random reads, for the byte and
# operation limits. One JSON file per configuration; caches dropped before each.
#     bash experiments/network/fio_mixed.sh > /data/exp04-results/exp90-fio.log 2>&1
# OUT, SIZE (default 20G) and RUNTIME (seconds per configuration, default 30) change the results directory, the
# file and the length (the smoke: SIZE=2G RUNTIME=5).
set -u
OUT=${OUT:-/data/exp04-results/exp90-fio}
SIZE=${SIZE:-20G}
RUNTIME=${RUNTIME:-30}
F=/data/exp90-fio.dat
rm -rf "$OUT"; mkdir -p "$OUT"
command -v fio > /dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q fio > /dev/null 2>&1
fio --version > "$OUT/fio-version.txt" || { echo "no fio; failed"; exit 1; }
echo "$(date -u +%T) laying out $SIZE"
fio --name=layout --filename="$F" --size="$SIZE" --rw=write --bs=1M --direct=1 --ioengine=libaio --iodepth=8 \
  --output-format=json > "$OUT/layout.json" || { echo "layout failed"; exit 1; }
COMMON="--filename=$F --size=$SIZE --direct=1 --ioengine=libaio --time_based --runtime=$RUNTIME --ramp_time=5 --randrepeat=0"
READ="--rw=read --bs=1M --iodepth=8"
WRITE="--rw=randwrite --bs=8k --iodepth=32"
run() {  # $1: name; the rest: fio's job options (more than one --name runs the jobs at once)
  local name=$1; shift
  sync; echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null
  # shellcheck disable=SC2086
  fio $COMMON --output-format=json "$@" > "$OUT/$name.json" || { echo "$name failed"; exit 1; }
  python3 - "$OUT/$name.json" <<'EOF'
import json, sys
for job in json.load(open(sys.argv[1]))['jobs']:
    for side in ('read', 'write'):
        s = job[side]
        if s['io_bytes']:
            print(f"{job['jobname']} {side}: {s['iops']:,.0f} ops/s, {s['bw_bytes'] / 1e6:,.1f} MB/s")
EOF
}
# shellcheck disable=SC2086
run seqread-alone --name=seqread $READ
# shellcheck disable=SC2086
run randwrite-alone --name=randwrite $WRITE
# Both at once: two jobs in one fio process, on the same file, each with its own queue.
# shellcheck disable=SC2086
run mixed --name=seqread $READ --name=randwrite $WRITE
# shellcheck disable=SC2086
run seqwrite-alone --name=seqwrite --rw=write --bs=1M --iodepth=8
# shellcheck disable=SC2086
run randread-alone --name=randread --rw=randread --bs=8k --iodepth=32
rm -f "$F"
echo "$(date -u +%T) done"
